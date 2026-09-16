"""Bounded, read-only observation of the persisted paper protection heartbeat."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

MAX_PAYLOAD = 65_536
MAX_AGE_SECONDS = 15
FUTURE_TOLERANCE_SECONDS = 5
# Watchlist entries this check will read before giving up. The heartbeat is already
# bounded; this bounds the loop independently so a malformed payload cannot cost time.
MAX_WATCHLIST = 64


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.utcoffset() is not None else None
    except (ValueError, OverflowError):
        return None


def _blind_through_an_open_session(payload: dict, now: datetime) -> bool:
    """Whether every instrument on the watchlist is stale while its market is open.

    A pilot with a dead feed is not idle, it is blind, and the two look identical from
    outside: it keeps its heartbeat, keeps deciding, and records a well-formed abstention
    every cadence because no specialist has anything to say. A whole session of those sits
    in the journal looking exactly like considered restraint.

    The existing feed-loss halt does not cover it. That one fires when an *open position*
    cannot be priced - which is the right rule for protecting a position, and no rule at
    all for a book that is flat. The pilot has never held one.

    Two deliberate narrowings, because a health check that cries wolf gets ignored:

    * **Every** instrument must be stale, not any. One stale symbol is a provider gap; the
      whole watchlist at once is the feed.
    * The market must be in regular hours. Staleness outside them is the normal state of a
      closed exchange, and reporting it would make the container unhealthy every night and
      teach a reader to disregard the signal.
    """
    watchlist = payload.get("watchlist")
    if not isinstance(watchlist, list) or not watchlist or len(watchlist) > MAX_WATCHLIST:
        return False
    markets: set[str] = set()
    for entry in watchlist:
        if not isinstance(entry, dict) or entry.get("fresh") is not False:
            return False
        market = entry.get("market")
        if not isinstance(market, str) or len(market) > 32:
            return False
        markets.add(market)
    from quant_ai.domain.models import Market
    from quant_ai.execution.session import MarketCalendar, MarketState

    calendar = MarketCalendar()
    for name in markets:
        try:
            market = Market(name)
        except ValueError:
            return False
        if calendar.state(market, now) != MarketState.REGULAR_HOURS:
            return False
    return True


def protection_health(database: Path, tenant: str, *, now: datetime | None = None) -> dict:
    """Exit/readiness policy is left to the caller; never return raw stored contents.

    A voluntary halt has a live protection heartbeat but is an unhealthy availability
    observation. This must not be used to restart protection or automatically resume.
    """
    now = now or datetime.now(timezone.utc)
    if now.utcoffset() is None:
        raise ValueError("Health-check clock must include a timezone")
    reasons: list[str] = []
    payload: dict = {}
    stored_at = payload_at = None
    try:
        with closing(sqlite3.connect(
            f"{database.resolve().as_uri()}?mode=ro", uri=True, timeout=1,
        )) as db:
            db.execute("PRAGMA query_only=ON")
            row = db.execute(
                "SELECT substr(updated_at,1,65), substr(payload,1,?) "
                "FROM pilot_runtime WHERE tenant_id=?", (MAX_PAYLOAD + 1, tenant),
            ).fetchone()
        if row is None:
            reasons.append("heartbeat_missing")
        else:
            stored_at = _timestamp(row[0])
            raw = row[1]
            if not isinstance(raw, str) or len(raw) > MAX_PAYLOAD:
                reasons.append("heartbeat_payload_invalid")
            else:
                try:
                    decoded = json.loads(raw)
                    if not isinstance(decoded, dict):
                        raise TypeError("Expected object")
                    payload = decoded
                except (ValueError, TypeError, RecursionError):
                    reasons.append("heartbeat_payload_invalid")
            payload_at = _timestamp(payload.get("updatedAt"))
    except (sqlite3.Error, OSError, UnicodeError):
        reasons.append("heartbeat_storage_unavailable")

    def age(timestamp: datetime | None, reason: str) -> float | None:
        seconds = (now - timestamp).total_seconds() if timestamp else None
        if seconds is None or not -FUTURE_TOLERANCE_SECONDS <= seconds <= MAX_AGE_SECONDS:
            reasons.append(reason)
        return round(seconds, 3) if seconds is not None else None

    stored_age = age(stored_at, "stored_heartbeat_stale_or_invalid")
    payload_age = age(payload_at, "payload_heartbeat_stale_or_invalid")
    if stored_at is not None and payload_at is not None and stored_at != payload_at:
        reasons.append("heartbeat_timestamp_mismatch")
    if payload.get("mode") != "paper":
        reasons.append("paper_mode_missing")
    if payload.get("status") != "running":
        reasons.append("engine_not_running")
    # bool is deliberately checked by identity; 0 and absent are not a known state.
    halted = payload.get("halted")
    if halted is not True and halted is not False:
        reasons.append("halt_state_unknown")
    if _blind_through_an_open_session(payload, now):
        reasons.append("market_data_stale_during_session")
    alive = not reasons
    if halted is True:
        reasons.append("engine_halted")
    return {
        "status": "observation_ok" if not reasons else "unhealthy",
        "heartbeat": "ok" if alive else "invalid_or_stale",
        "reasons": reasons,
        "age_seconds": stored_age,
        "payload_age_seconds": payload_age,
        "observed_at": stored_at.isoformat() if stored_at is not None else None,
        "halted": halted if halted is True or halted is False else None,
        "checked_at": now.isoformat(),
        "note": "Persisted paper protection observation, plus whether the whole watchlist "
                "is stale while its market is open; not execution or strategy readiness",
    }
