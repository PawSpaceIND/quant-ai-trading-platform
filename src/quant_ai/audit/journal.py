from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

# The chain is hash-linked by sequence, not by list position, so keeping only the newest
# events bounds a months-long session without breaking the links that are retained.
RETAINED_AUDIT_EVENTS = 5_000


@dataclass(frozen=True)
class AuditEvent:
    sequence: int
    timestamp: datetime
    event_type: str
    payload: dict[str, Any]
    previous_hash: str
    event_hash: str


class InMemoryAuditJournal:
    def __init__(self, *, retained: int = RETAINED_AUDIT_EVENTS) -> None:
        if retained < 1:
            raise ValueError("retained audit events must be positive")
        self._events: deque[AuditEvent] = deque(maxlen=retained)
        # Sequence and link state are tracked separately from the retained window so a
        # dropped event never lets a sequence number or a previous hash be reused.
        self._sequence = 0
        self._last_hash = "GENESIS"

    def append(self, event_type: str, payload: dict[str, Any]) -> AuditEvent:
        sequence = self._sequence + 1
        previous_hash = self._last_hash
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
        self._sequence = sequence
        self._last_hash = event_hash
        return event

    def events(self) -> tuple[AuditEvent, ...]:
        return tuple(self._events)

    def verify_chain(self) -> bool:
        """Verify the retained window: every hash recomputes and every link holds.

        The window can start mid-chain once the cap has dropped older events, so only a
        window that still contains the first event is anchored to ``GENESIS``.
        """
        previous: str | None = None
        for event in self._events:
            if previous is None:
                if event.sequence == 1 and event.previous_hash != "GENESIS":
                    return False
            elif event.previous_hash != previous:
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
