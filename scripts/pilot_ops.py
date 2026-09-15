"""Read-only health and consistent SQLite backups; restore drill never overwrites source."""
import argparse
import hashlib
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["health", "reconcile", "backup", "restore-drill", "pilot-check"])
    parser.add_argument("--database", type=Path)
    parser.add_argument("--tenant", default="ghost")
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--evidence", type=Path)
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
    else:
        if not args.destination:
            parser.error("--destination required; use a new path")
        if args.action == "restore-drill":
            manifest = json.loads(args.database.with_suffix(args.database.suffix + ".manifest.json").read_text())
            if hashlib.sha256(args.database.read_bytes()).hexdigest() != manifest["sha256"]:
                raise ValueError("Backup checksum mismatch")
        result = backup(args.database, args.destination)
    print(json.dumps(result, indent=2))
