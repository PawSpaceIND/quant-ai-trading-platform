"""Validation shared by stream ingestion and final market-data readers."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from decimal import Decimal


def utc_time(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("invalid_tick_timestamp")
    # Generic internal ticks historically use naive UTC. Provider adapters must
    # resolve their own timestamp semantics before constructing an internal tick.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def tick_value_issue(tick) -> str | None:
    for field in ("ltp", "volume", "bid", "ask"):
        value = getattr(tick, field, None)
        if value is None and field in {"bid", "ask"}:
            continue
        if not isinstance(value, Decimal) or not value.is_finite() or not math.isfinite(float(value)):
            return "invalid_tick_values"
        if value < 0 or (field == "ltp" and float(value) <= 0):
            return "invalid_tick_values"
    if tick.bid is not None and tick.ask is not None and tick.bid > 0 and tick.ask > 0 and tick.bid > tick.ask:
        return "crossed_tick_quotes"
    return None
