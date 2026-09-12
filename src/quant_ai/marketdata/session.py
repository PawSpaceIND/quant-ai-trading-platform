from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

from quant_ai.domain.models import Market


@dataclass(frozen=True)
class MarketSession:
    market: Market
    timezone: str
    open_time: time
    close_time: time

    def is_open(self, timestamp: datetime) -> bool:
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        local = timestamp.astimezone(ZoneInfo(self.timezone))
        if local.weekday() >= 5:
            return False
        return self.open_time <= local.time().replace(tzinfo=None) < self.close_time


SESSIONS = {
    Market.INDIA: MarketSession(Market.INDIA, "Asia/Kolkata", time(9, 15), time(15, 30)),
    Market.USA: MarketSession(Market.USA, "America/New_York", time(9, 30), time(16, 0)),
}
