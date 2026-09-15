"""Append-only, hash-chained evidence log.

One line of canonical JSON per record, each carrying the digest of the record before it.
Editing or deleting a line after the fact breaks the chain, so a reader can tell whether
the file it is trusting is the file that was written. Used for the research trial register
and for operator overrides that clear a durable fault halt.

Paper-only bookkeeping: nothing recorded here authorises an order.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = "pramana.evidence_log.v1"
GENESIS = "GENESIS"


def _canonical(record: dict[str, Any]) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(body: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(body).encode()).hexdigest()


def read_records(path: str | Path) -> list[dict[str, Any]]:
    """Every record in the log, oldest first. A missing file is an empty log."""
    target = Path(path)
    if not target.exists():
        return []
    records: list[dict[str, Any]] = []
    for number, line in enumerate(target.read_text(encoding="utf-8").splitlines(), start=1):
        text = line.strip()
        if not text:
            continue
        try:
            entry = json.loads(text)
        except ValueError as error:
            raise ValueError(f"evidence log line {number} is not valid JSON") from error
        if not isinstance(entry, dict) or not entry:
            raise ValueError(f"evidence log line {number} is not a record")
        records.append(entry)
    return records


def verify_chain(records: list[dict[str, Any]]) -> bool:
    """True when every record is in sequence and hashes to its recorded digest."""
    previous = GENESIS
    for index, entry in enumerate(records, start=1):
        body = {key: value for key, value in entry.items() if key != "sha256"}
        if body.get("schema") != SCHEMA or body.get("sequence") != index:
            return False
        if body.get("previous_sha256") != previous:
            return False
        if _digest(body) != entry.get("sha256"):
            return False
        previous = str(entry["sha256"])
    return True


def append_record(
    path: str | Path,
    event_type: str,
    payload: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Append one record and return it. Refuses to extend a log whose chain is broken."""
    if not event_type.strip():
        raise ValueError("evidence log record needs an event type")
    target = Path(path)
    records = read_records(target)
    if not verify_chain(records):
        raise ValueError(f"evidence log chain is broken: {target}")
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("evidence log timestamps must be timezone-aware")
    body = {
        "schema": SCHEMA,
        "sequence": len(records) + 1,
        "recorded_at": moment.astimezone(timezone.utc).isoformat(),
        "event_type": event_type.strip(),
        "payload": json.loads(json.dumps(payload, sort_keys=True, default=str)),
        "previous_sha256": records[-1]["sha256"] if records else GENESIS,
    }
    entry = {**body, "sha256": _digest(body)}
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(_canonical(entry) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return entry
