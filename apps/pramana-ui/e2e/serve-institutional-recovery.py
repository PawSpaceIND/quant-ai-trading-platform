"""Disposable loopback browser fixture running the actual recovery API and stores.

Not a deployment launcher. --seed requires an empty caller-created temporary root;
--serve requires its explicit fixture marker. Never prints a possession secret.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
os.environ["TRADING_LIVE_MONEY_ACTIVE"] = "false"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--seed", action="store_true")
    mode.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    root = args.root.resolve()
    if not root.is_dir() or not root.name.startswith("pramana-browser-recovery-"):
        raise ValueError("Disposable browser fixture directory required")
    from test_institutional_swarm_bridge import BridgeHarness
    from quant_ai.security.persistent_api_keys import PersistentApiKeyRegistry
    from quant_ai.operations.institutional_operator import InstitutionalRecoveryOperations
    if args.seed:
        if any(root.iterdir()):
            raise ValueError("Refusing to overwrite an existing fixture")
        h = BridgeHarness(root)
        original = h.broker.submit_with_evidence
        def interrupted(*a, **kw):
            original(*a, **kw)
            raise ValueError("synthetic interrupted browser fill")
        h.broker.submit_with_evidence = interrupted
        result = h.execute()
        assert result.order_state.value == "SUBMISSION_UNCERTAIN"
        assert len(h.broker.ledger_entries("tenant")) == 1
        pid = h.programs.db.execute("SELECT program_id FROM execution_programs").fetchone()[0]
        with PersistentApiKeyRegistry(root / "credentials.sqlite", create=True) as keys:
            for name, scopes in (("read.key", ("institutional.recovery.read",)),
                ("apply.key", ("institutional.recovery.read", "institutional.recovery.apply"))):
                raw, _ = keys.issue("tenant", scopes=scopes, expires_at=datetime.now(timezone.utc)+timedelta(hours=1))
                fd = os.open(root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as out:
                    out.write(raw)
        h.close()
        (root / "fixture.json").write_text(json.dumps({"schema":"synthetic-browser-recovery.v1", "programId":pid}))
        return
    marker = json.loads((root / "fixture.json").read_text())
    if marker["schema"] != "synthetic-browser-recovery.v1":
        raise ValueError("Synthetic fixture marker required")
    import uvicorn
    from quant_ai.api.recovery_app import create_recovery_app
    h = BridgeHarness(root)
    h.runtime.kill_switch.engage("synthetic browser test halt")
    keys = PersistentApiKeyRegistry(root / "credentials.sqlite")
    operations = InstitutionalRecoveryOperations(h.runtime, root / "operator-audit.sqlite")
    app = create_recovery_app(keys=keys, operations={"tenant": operations}, rate_limit_per_minute=500)
    @app.get("/fixture/observations")
    def observations():
        return {"brokerOrders":len(h.broker.ledger_entries("tenant")),
            "cash":str(h.journal.native_balance("tenant", "CASH_AVAILABLE", "INR")),
            "securities":str(h.journal.native_balance("tenant", "SECURITIES_COST", "INR")),
            "haltEngaged":h.runtime.kill_switch.engaged, "auditEvents":len(operations._records())}
    try:
        uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, log_level="warning")
    finally:
        operations.close(); keys.close(); h.close()


if __name__ == "__main__":
    main()
