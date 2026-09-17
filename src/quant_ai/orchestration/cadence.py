from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from quant_ai.marketdata.tick_integrity import tick_value_issue, utc_time
from quant_ai.marketdata.ticker_stream import LiveTick, TickBuffer

LOGGER = logging.getLogger("quant_ai.cadence")


@dataclass(frozen=True)
class DecisionCadence:
    atlas_cycle: timedelta = timedelta(minutes=10)
    founder_daily_brief: timedelta = timedelta(days=1)
    founder_weekly_review: timedelta = timedelta(days=7)
    founder_monthly_review: timedelta = timedelta(days=30)
    founder_yearly_review: timedelta = timedelta(days=365)

    def __post_init__(self) -> None:
        if self.atlas_cycle < timedelta(minutes=1):
            raise ValueError("atlas cadence must be at least one minute")


class CadenceMarketReader:
    """Reads the newest eligible websocket tick at the requested analysis cutoff."""

    def __init__(self, buffer: TickBuffer, max_tick_age: timedelta = timedelta(minutes=2)) -> None:
        if max_tick_age <= timedelta(0):
            raise ValueError("max_tick_age must be positive")
        self.buffer = buffer
        self.max_tick_age = max_tick_age

    def market_data_status(
        self, symbol: str, now: datetime | None = None
    ) -> tuple[LiveTick | None, str | None]:
        tick = self.buffer.latest(symbol)
        if tick is None:
            return None, "Missing Market Data"
        current = utc_time(now or datetime.now(timezone.utc))
        try:
            observed = utc_time(tick.observed_at)
        except (TypeError, ValueError, AttributeError):
            return None, "Invalid Market Data"
        if tick_value_issue(tick) or tick.symbol != symbol:
            return None, "Invalid Market Data"
        newest_at = observed
        deferred = observed > current
        if deferred:
            # Preserve the fixed evidence cutoff across provider/LLM awaits. Never
            # compare a post-cutoff tick by advancing the clock to make it pass.
            select = getattr(self.buffer, "latest_at_or_before", None)
            try:
                tick = select(symbol, current) if callable(select) else None
            except (TypeError, ValueError, AttributeError):
                return None, "Invalid Market Data"
        if tick is None:
            issue = "Future Market Data"
            observed = None
        else:
            try:
                observed = utc_time(tick.observed_at)
            except (TypeError, ValueError, AttributeError):
                return None, "Invalid Market Data"
            # A custom/replaced selector is not trusted to enforce the boundary.
            if tick_value_issue(tick) or tick.symbol != symbol:
                issue = "Invalid Market Data"
            elif observed > current:
                issue = "Future Market Data"
            elif current - observed > self.max_tick_age:
                issue = "Stale Market Data"
            else:
                issue = None
        if deferred:
            LOGGER.info(
                "cadence_tick_cutoff symbol=%r cutoff=%s newest_at=%s selected_at=%s "
                "wall_checked_at=%s result=%s",
                symbol[:80], current.isoformat(), newest_at.isoformat(),
                observed.isoformat() if observed is not None else "unavailable",
                datetime.now(timezone.utc).isoformat(), issue or "eligible",
            )
        return (None, issue) if issue else (tick, None)

    def latest_for_consensus(self, symbol: str, now: datetime | None = None) -> LiveTick | None:
        tick, _ = self.market_data_status(symbol, now)
        return tick
