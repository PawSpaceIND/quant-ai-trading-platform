from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any


@dataclass(frozen=True)
class AuditEvent:
    sequence: int
    timestamp: datetime
    event_type: str
    payload: dict[str, Any]
    previous_hash: str
    event_hash: str


class InMemoryAuditJournal:
    def __init__(self) -> None:
        self._events: list[AuditEvent] = []

    def append(self, event_type: str, payload: dict[str, Any]) -> AuditEvent:
        sequence = len(self._events) + 1
        previous_hash = self._events[-1].event_hash if self._events else "GENESIS"
        timestamp = datetime.now(timezone.utc)
        canonical = json.dumps(
            {
                "sequence": sequence,
                "timestamp": timestamp.isoformat(),
                "event_type": event_type,
                "payload": payload,
                "previous_hash": previous_hash,
            },
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        )
        event_hash = sha256(canonical.encode()).hexdigest()
        event = AuditEvent(sequence, timestamp, event_type, payload, previous_hash, event_hash)
        self._events.append(event)
        return event

    def events(self) -> tuple[AuditEvent, ...]:
        return tuple(self._events)

    def verify_chain(self) -> bool:
        previous = "GENESIS"
        for event in self._events:
            if event.previous_hash != previous:
                return False
            canonical = json.dumps(
                {
                    "sequence": event.sequence,
                    "timestamp": event.timestamp.isoformat(),
                    "event_type": event.event_type,
                    "payload": event.payload,
                    "previous_hash": event.previous_hash,
                },
                sort_keys=True,
                default=str,
                separators=(",", ":"),
            )
            if sha256(canonical.encode()).hexdigest() != event.event_hash:
                return False
            previous = event.event_hash
        return True
