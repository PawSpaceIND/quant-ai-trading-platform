from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from quant_ai.marketdata.tick_integrity import tick_value_issue, utc_time
from quant_ai.marketdata.ticker_stream import LiveTick, TickBuffer


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
    """Reads the newest websocket tick immediately before agent consensus."""

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
        except (TypeError, ValueError):
            return None, "Invalid Market Data"
        if tick_value_issue(tick):
            return None, "Invalid Market Data"
        if observed > current:
            return None, "Future Market Data"
        if current - observed > self.max_tick_age:
            return None, "Stale Market Data"
        return tick, None

    def latest_for_consensus(self, symbol: str, now: datetime | None = None) -> LiveTick | None:
        tick, _ = self.market_data_status(symbol, now)
        return tick
