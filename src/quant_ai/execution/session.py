from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from enum import Enum
from zoneinfo import ZoneInfo

from quant_ai.domain.models import Market


class MarketState(str, Enum):
    PRE_MARKET = "PRE_MARKET"
    REGULAR_HOURS = "REGULAR_HOURS"
    POST_MARKET = "POST_MARKET"
    CLOSED = "CLOSED"


@dataclass(frozen=True)
class MarketCalendar:
    holidays: dict[Market, frozenset[date]] = field(default_factory=dict)

    def state(self, market: Market, timestamp: datetime) -> MarketState:
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        timezone, pre_open, regular_open, regular_close, post_close = self._session(market)
        local = timestamp.astimezone(ZoneInfo(timezone))
        if local.weekday() >= 5 or local.date() in self.holidays.get(market, frozenset()):
            return MarketState.CLOSED
        local_time = local.time().replace(tzinfo=None)
        if pre_open <= local_time < regular_open:
            return MarketState.PRE_MARKET
        if regular_open <= local_time < regular_close:
            return MarketState.REGULAR_HOURS
        if regular_close <= local_time < post_close:
            return MarketState.POST_MARKET
        return MarketState.CLOSED

    @staticmethod
    def _session(market: Market) -> tuple[str, time, time, time, time]:
        if market == Market.INDIA:
            return "Asia/Kolkata", time(9), time(9, 15), time(15, 30), time(16)
        if market == Market.USA:
            return "America/New_York", time(4), time(9, 30), time(16), time(20)
        raise ValueError("unsupported_market_calendar")
