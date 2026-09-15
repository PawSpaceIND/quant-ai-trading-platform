"""Periodic consistent ledger backups with a retention cap.

Invokes the existing ``scripts/pilot_ops.py backup`` - SQLite's online backup API
against a read-only connection, integrity-checked, with a sha256 manifest - on an
interval, then keeps only the newest ``--keep`` copies. The online backup API is
designed to run against a live WAL database, so the engine is never paused or locked.

Runs as its own small Compose service on the engine image so the pilot needs no host
cron; see deploy/docker-compose.yml. A failing cycle is logged and the loop continues:
a backup that cannot be taken must not take the service down with it.

An on-instance backup is not a backup. Copy the directory off the host as well; see
docs/VPS_GHOST_DEPLOYMENT.md.
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

PILOT_OPS = Path(__file__).resolve().parent / "pilot_ops.py"
BACKUP_PREFIX = "pramana-"
BACKUP_SUFFIX = ".db"
MANIFEST_SUFFIX = ".manifest.json"
DEFAULT_INTERVAL_SECONDS = 86_400
DEFAULT_KEEP = 14
BACKUP_TIMEOUT_SECONDS = 1_800

logger = logging.getLogger("quant_ai.scheduled_backup")


def backup_name(moment: datetime) -> str:
    """Timestamped name; the UTC stamp sorts lexicographically and so chronologically."""
    return f"{BACKUP_PREFIX}{moment.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}{BACKUP_SUFFIX}"


def run_backup(database: Path, directory: Path, moment: datetime) -> dict:
    """Take one backup through pilot_ops; returns its manifest."""
    destination = directory / backup_name(moment)
    completed = subprocess.run(
        [
            sys.executable,
            str(PILOT_OPS),
            "backup",
            "--database",
            str(database),
            "--destination",
            str(destination),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=BACKUP_TIMEOUT_SECONDS,
    )
    return json.loads(completed.stdout)


def prune(directory: Path, keep: int) -> tuple[Path, ...]:
    """Delete all but the newest ``keep`` backups, each with its manifest."""
    if keep < 1:
        raise ValueError("keep must be at least 1")
    backups = sorted(directory.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}"))
    removed = []
    for stale in backups[: max(len(backups) - keep, 0)]:
        stale.with_name(stale.name + MANIFEST_SUFFIX).unlink(missing_ok=True)
        stale.unlink(missing_ok=True)
        removed.append(stale)
    return tuple(removed)


def cycle(database: Path, directory: Path, keep: int) -> bool:
    """One backup plus prune. Any failure is reported, never raised."""
    try:
        manifest = run_backup(database, directory, datetime.now(timezone.utc))
        removed = prune(directory, keep)
        logger.info(
            "backup_complete backup=%s sha256=%s integrity=%s pruned=%d",
            manifest["backup"],
            manifest["sha256"],
            manifest["integrity"],
            len(removed),
        )
        return True
    except subprocess.CalledProcessError as error:
        logger.error("backup_failed returncode=%s stderr=%s", error.returncode, error.stderr)
    except Exception:
        logger.exception("backup_failed")
    return False


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--interval-seconds", type=int, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP)
    parser.add_argument("--once", action="store_true", help="take a single backup and exit")
    args = parser.parse_args(argv)
    if args.interval_seconds < 1:
        parser.error("--interval-seconds must be positive")
    if args.keep < 1:
        parser.error("--keep must be at least 1")
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    if args.once:
        return 0 if cycle(args.database, args.directory, args.keep) else 1
    while True:
        cycle(args.database, args.directory, args.keep)
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
