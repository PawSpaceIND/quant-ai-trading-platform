"""Capture selected external Kite funds/net positions through GET-only requests."""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from quant_ai.execution.external_account_snapshot import capture_external_account
from quant_ai.execution.live_brokers import ReadOnlyJsonTransport


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True, help="Expected Kite user ID; checked before and after reads")
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--output", type=Path, required=True, help="New private report; existing files are not overwritten")
    args = parser.parse_args()
    key, token = os.environ.get("KITE_API_KEY"), os.environ.get("KITE_ACCESS_TOKEN")
    if not key or not token:
        parser.error("KITE_API_KEY and KITE_ACCESS_TOKEN are required")
    transport = ReadOnlyJsonTransport("https://api.kite.trade", {
        "X-Kite-Version": "3", "Authorization": f"token {key}:{token}"})
    try:
        report = capture_external_account(transport, args.account_id, args.tenant_id)
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump(report, output, ensure_ascii=False, allow_nan=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
    except (OSError, ValueError, ArithmeticError):
        print("External account capture failed. Check account, session, connectivity and private output path.", file=sys.stderr)
        return 2
    print(json.dumps({"status": report["status"], "sha256": report["sha256"],
                      "netPositionCount": len(report["positions"]), "paperAccountReconciled": False,
                      "depositoryHoldingsCovered": False, "liveExecutionEnabled": False}))
    return 0 if report["status"] == "consistent" else 2


if __name__ == "__main__":
    raise SystemExit(main())
