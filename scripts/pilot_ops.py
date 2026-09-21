"""Read-only health and consistent SQLite backups; restore drill never overwrites source."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import RLock


def backup(source: Path, destination: Path) -> dict:
    if not source.is_file():
        raise ValueError("Source database does not exist")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive reservation prevents accidental replacement of prior evidence.
    with destination.open("xb"):
        pass
    destination.chmod(0o600)
    try:
        with closing(sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)) as src, closing(sqlite3.connect(destination)) as out:
            src.backup(out)
            out.execute("PRAGMA journal_mode=DELETE")
            if out.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Backup integrity check failed")
        digest = hashlib.sha256(destination.read_bytes()).hexdigest()
        manifest = {"source": str(source), "backup": str(destination), "sha256": digest,
                    "created_at": datetime.now(timezone.utc).isoformat(), "integrity": "ok"}
        destination.with_suffix(destination.suffix + ".manifest.json").write_text(json.dumps(manifest, indent=2))
        return manifest
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def health(database: Path, tenant: str) -> dict:
    from quant_ai.operations.health import protection_health
    return protection_health(database, tenant)


def premarket(database: Path, tenant: str, env=None, now=None) -> tuple[str, bool]:
    """One line per pre-market condition; True when nothing failed."""
    from quant_ai.operations.premarket import load_engine_records, premarket_checks, render
    payload, manifest = load_engine_records(database, tenant)
    checks = premarket_checks(payload, manifest, os.environ if env is None else env,
                              now or datetime.now(timezone.utc))
    return render(checks), all(check.state != "FAIL" for check in checks)


class ReadOnlyLedger:
    """The two attributes ``decision_journal.load_rows`` needs, over a read-only connection.

    ``PaperBrokerService`` switches the ledger to WAL and creates its schema on open, so it
    cannot be pointed at the live ledger from an operator shell without writing to it. This
    opens ``mode=ro`` and deliberately not ``query_only``: the journal's ``CREATE ... IF NOT
    EXISTS`` is a no-op on a table that exists and SQLite tolerates it on a read-only
    connection, while any genuine write still fails.
    """

    def __init__(self, database: Path) -> None:
        self._lock = RLock()
        self._connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True, timeout=1)
        self._connection.row_factory = sqlite3.Row

    def has_table(self, name: str) -> bool:
        return self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None

    def close(self) -> None:
        self._connection.close()


def missed(database: Path, tenant: str, session_date: str | None = None,
           threshold: Decimal | None = None, now: datetime | None = None) -> str:
    """The session's holds scored against their 60-minute forward returns, as terminal text."""
    from quant_ai.analytics import missed_opportunities as report
    from quant_ai.analytics.decision_journal import TABLE
    moment = now or datetime.now(timezone.utc)
    threshold = report.DEFAULT_THRESHOLD if threshold is None else threshold
    ledger = ReadOnlyLedger(database)
    try:
        if ledger.has_table(TABLE):
            built = report.build_report(ledger, tenant_id=tenant, now=moment,
                                        session_date=session_date, threshold=threshold)
        else:
            # A ledger that never journaled: nothing was held, so nothing was missed.
            built = report.summarize(
                [], tenant_id=tenant, now=moment, threshold=threshold,
                session_date=session_date or moment.astimezone(report.IST).date().isoformat(),
            )
    finally:
        ledger.close()
    return report.render(built)


def plan(database: Path, session_date: str | None = None) -> str:
    """The pre-open session plan beside the ledger (or the newest one), as terminal text."""
    from quant_ai.agents import strategist
    directory = Path(os.environ.get("PRAMANA_SESSION_PLAN_DIR") or database.resolve().parent / "session-plans")
    if session_date:
        found = strategist.load_plan(strategist.plan_path(directory, session_date))
    else:
        found = strategist.latest_plan(directory)
    if found is None:
        return f"no session plan {'for ' + session_date if session_date else 'written'} under {directory}"
    return strategist.render(found)


def fraction(text: str) -> Decimal:
    """A ``--threshold`` such as 0.01; argparse only reports ValueError-family failures."""
    try:
        return Decimal(text)
    except InvalidOperation as error:
        raise argparse.ArgumentTypeError(f"not a decimal fraction: {text}") from error


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["health", "premarket", "reconcile", "backup", "restore-drill", "pilot-check", "missed", "plan"])
    parser.add_argument("--database", type=Path)
    parser.add_argument("--tenant", default="ghost")
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--date", help="IST session date YYYY-MM-DD for `missed` (default today) or `plan` (default newest)")
    parser.add_argument("--threshold", type=fraction, help="missed-move threshold as a fraction; default 0.01")
    args = parser.parse_args()
    if args.action == "pilot-check":
        if not args.evidence:
            parser.error("pilot-check requires --evidence external-gates.json")
        from quant_ai.operations.pilot_gate import external_gate_report
        evidence = json.loads(args.evidence.read_text())
        result = external_gate_report(evidence, evidence_root=args.evidence.resolve().parent)
        if args.destination:
            # Create privately from the first byte; never replace reviewed evidence.
            fd = os.open(args.destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as output:
                json.dump(result, output, indent=2)
                output.write("\n")
        print(json.dumps(result, indent=2))
        raise SystemExit(0 if result["ready"] else 2)
    if not args.database:
        parser.error(f"{args.action} requires --database")
    if args.action == "missed":
        print(missed(args.database, args.tenant, session_date=args.date, threshold=args.threshold))
        raise SystemExit(0)
    if args.action == "plan":
        print(plan(args.database, session_date=args.date))
        raise SystemExit(0)
    if args.action == "reconcile":
        from quant_ai.execution.reconciliation import reconcile_paper
        with sqlite3.connect(f"{args.database.resolve().as_uri()}?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            result = reconcile_paper(db, args.tenant)
        print(json.dumps(result, indent=2))
        raise SystemExit(0 if result["status"] == "matched" else 2)
    if args.action == "health":
        result = health(args.database, args.tenant)
        print(json.dumps(result, indent=2))
        raise SystemExit(0 if result["status"] == "observation_ok" else 2)
    if args.action == "premarket":
        report, ready = premarket(args.database, args.tenant)
        print(report)
        raise SystemExit(0 if ready else 2)
    else:
        if not args.destination:
            parser.error("--destination required; use a new path")
        if args.action == "restore-drill":
            manifest = json.loads(args.database.with_suffix(args.database.suffix + ".manifest.json").read_text())
            if hashlib.sha256(args.database.read_bytes()).hexdigest() != manifest["sha256"]:
                raise ValueError("Backup checksum mismatch")
        result = backup(args.database, args.destination)
    print(json.dumps(result, indent=2))
