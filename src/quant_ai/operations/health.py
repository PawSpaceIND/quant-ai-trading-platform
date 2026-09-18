"""Bounded, read-only observation of the persisted paper protection heartbeat."""
from __future__ import annotations

import json
import os
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


def _market_data_observation(payload: dict, now: datetime) -> dict:
    """Classify declared freshness within the SAME configured exchange sessions.

    Closed instruments cannot mask an entirely blind open subset. One stale symbol
    among fresh OPEN instruments is still a partial provider gap, not a dead-feed
    alarm. This reads persisted booleans; it does not independently verify quotes.
    Unknown or malformed observations never become claims of fresh market data.
    """
    from quant_ai.domain.models import Market
    from quant_ai.execution.session import (
        INDIA_EXCHANGE_SESSIONS,
        MarketCalendar,
        MarketState,
        default_holidays,
        holidays_from_json,
    )

    def result(state: str, *, reason: str | None = None, opened=None, fresh=None) -> dict:
        return {"state": state, "reason": reason, "open_instruments": opened,
                "fresh_open_instruments": fresh}

    # Reuse the daemon/monitor's parser and default dates; do not duplicate hours
    # or silently ignore a malformed configured closure when deciding availability.
    raw = os.getenv("PRAMANA_HOLIDAYS_JSON") or "{}"
    try:
        if len(raw) > MAX_PAYLOAD:
            raise ValueError("calendar_config_oversized")
        calendar = MarketCalendar(holidays_from_json(json.loads(raw), default_holidays()))
    except (ValueError, TypeError, OverflowError, RecursionError):
        return result("unknown", reason="calendar_configuration_invalid")

    watchlist = payload.get("watchlist")
    if not isinstance(watchlist, list) or not watchlist or len(watchlist) > MAX_WATCHLIST:
        return result("unknown", reason="watchlist_unreadable")
    open_freshness: list[bool] = []
    for entry in watchlist:
        if not isinstance(entry, dict) or type(entry.get("fresh")) is not bool:
            return result("unknown", reason="watchlist_entry_invalid")
        name = entry.get("market")
        exchange = entry.get("exchange")
        if not isinstance(name, str) or len(name) > 32:
            return result("unknown", reason="market_unresolved")
        if exchange is not None and (not isinstance(exchange, str) or len(exchange) > 32):
            return result("unknown", reason="exchange_unresolved")
        exchange = (exchange or "").strip().upper()
        try:
            market = Market(name)
            if market is Market.INDIA and exchange and exchange not in INDIA_EXCHANGE_SESSIONS:
                return result("unknown", reason="exchange_unresolved")
            state = calendar.state(market, now, exchange=exchange or None)
        except (ValueError, KeyError, TypeError, OverflowError):
            return result("unknown", reason="market_unresolved")
        if state is MarketState.REGULAR_HOURS:
            open_freshness.append(entry["fresh"])
    opened = len(open_freshness)
    fresh = sum(open_freshness)
    if not opened:
        return result("closed", opened=0, fresh=0)
    state = "blind" if fresh == 0 else "fresh" if fresh == opened else "partial"
    return result(state, opened=opened, fresh=fresh)


def _blind_through_an_open_session(payload: dict, now: datetime) -> bool:
    """Compatibility predicate; unknown data is not proof of a blind feed."""
    return _market_data_observation(payload, now)["state"] == "blind"


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
    market_data = _market_data_observation(payload, now)
    if market_data["state"] == "blind":
        reasons.append("market_data_stale_during_session")
    if market_data["reason"] == "calendar_configuration_invalid":
        reasons.append("market_calendar_invalid")
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
        "market_data": market_data,
        "note": "Persisted paper protection observation and declared open-session freshness; "
                "unknown data is not verified freshness; not execution or strategy readiness",
    }
