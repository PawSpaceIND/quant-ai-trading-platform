"""Zerodha Kite session records: daily expiry and atomic 0600 persistence.

Kite issues one access token per interactive login and invalidates it every day
at about 06:00 IST. An expired token does not fail loudly: the websocket simply
goes quiet, which pauses protective-stop enforcement. This module records when a
token was issued so the paper runtime can refuse a stale session before it
connects, and it never persists ``api_secret``.
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

INDIA_TZ = ZoneInfo("Asia/Kolkata")
DEFAULT_CUTOFF_HOUR_IST = 6
SESSION_FILE_NAME = "zerodha-session.json"
PERSISTED_FIELDS = ("user_id", "access_token", "issued_at", "login_time")


def _require_aware(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value


@dataclass(frozen=True)
class SessionRecord:
    """Identity of a Kite session; ``issued_at`` is when the access token was generated."""

    user_id: str
    access_token: str = field(repr=False)
    issued_at: datetime
    login_time: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.user_id, str) or not self.user_id.strip():
            raise ValueError("user_id is required")
        if not isinstance(self.access_token, str) or not self.access_token.strip():
            raise ValueError("access_token is required")
        _require_aware(self.issued_at, "issued_at")

    @classmethod
    def from_kite_session(
        cls, payload: Mapping[str, object], *, issued_at: datetime
    ) -> SessionRecord:
        """Keep only the identity fields of a ``generate_session`` response.

        The response also carries ``api_key``, ``public_token`` and ``refresh_token``;
        none of them are persisted.
        """
        login_time = payload.get("login_time")
        if isinstance(login_time, datetime):
            login_text: str | None = login_time.isoformat()
        else:
            login_text = str(login_time) if login_time else None
        return cls(
            user_id=str(payload.get("user_id") or ""),
            access_token=str(payload.get("access_token") or ""),
            issued_at=issued_at,
            login_time=login_text,
        )


def latest_cutoff(now: datetime, *, cutoff_hour_ist: int = DEFAULT_CUTOFF_HOUR_IST) -> datetime:
    """Most recent ``cutoff_hour_ist``:00 IST at or before ``now``, as an IST-aware datetime."""
    _require_aware(now, "now")
    if not 0 <= int(cutoff_hour_ist) <= 23:
        raise ValueError("cutoff_hour_ist must be between 0 and 23")
    local = now.astimezone(INDIA_TZ)
    cutoff = local.replace(hour=int(cutoff_hour_ist), minute=0, second=0, microsecond=0)
    if cutoff > local:
        cutoff -= timedelta(days=1)
    return cutoff


def next_cutoff(now: datetime, *, cutoff_hour_ist: int = DEFAULT_CUTOFF_HOUR_IST) -> datetime:
    """First ``cutoff_hour_ist``:00 IST strictly after ``now``: when a token issued now dies."""
    return latest_cutoff(now, cutoff_hour_ist=cutoff_hour_ist) + timedelta(days=1)


def is_expired(
    record: SessionRecord, now: datetime, *, cutoff_hour_ist: int = DEFAULT_CUTOFF_HOUR_IST
) -> bool:
    """True when the token was issued before the most recent daily cutoff at or before ``now``."""
    _require_aware(record.issued_at, "issued_at")
    return record.issued_at < latest_cutoff(now, cutoff_hour_ist=cutoff_hour_ist)


def write_session(path: Path, record: SessionRecord) -> Path:
    """Atomically write ``record`` to ``path`` with mode 0600 (temp file + rename).

    Only ``PERSISTED_FIELDS`` are written; there is no field for ``api_secret``.
    """
    target = Path(path)
    payload = {
        "user_id": record.user_id,
        "access_token": record.access_token,
        "issued_at": record.issued_at.astimezone(timezone.utc).isoformat(),
        "login_time": record.login_time,
    }
    if tuple(payload) != PERSISTED_FIELDS:  # pragma: no cover - guards future edits
        raise ValueError("session payload fields changed; review what is persisted")
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # mkstemp creates the file 0600 from the start, so the token is never world-readable.
    descriptor, temporary = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise
    return target


def read_session(path: Path) -> SessionRecord:
    """Load a session file; every field is validated and ``issued_at`` must be tz-aware."""
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"{source} is not valid JSON") from error
    fields = payload if isinstance(payload, dict) else {}
    issued_raw = fields.get("issued_at")
    if not isinstance(issued_raw, str) or not issued_raw.strip():
        raise ValueError(f"{source} has no issued_at (pramana zerodha-login writes it)")
    try:
        issued_at = datetime.fromisoformat(issued_raw.strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{source} has an unparseable issued_at") from error
    login_time = fields.get("login_time")
    try:
        return SessionRecord(
            user_id=str(fields.get("user_id") or ""),
            access_token=str(fields.get("access_token") or ""),
            issued_at=issued_at,
            login_time=str(login_time) if login_time else None,
        )
    except ValueError as error:
        raise ValueError(f"{source}: {error}") from error
