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


class GlobalVenue(str, Enum):
    INDIA = "INDIA"
    USA = "USA"
    TOKYO = "TOKYO"
    LONDON = "LONDON"
    FRANKFURT = "FRANKFURT"


@dataclass(frozen=True)
class SessionDefinition:
    timezone: str
    pre_open: time
    regular_open: time
    regular_close: time
    post_close: time


SESSIONS = {
    GlobalVenue.INDIA: SessionDefinition(
        "Asia/Kolkata", time(9), time(9, 15), time(15, 30), time(16)
    ),
    GlobalVenue.USA: SessionDefinition(
        "America/New_York", time(4), time(9, 30), time(16), time(20)
    ),
    GlobalVenue.TOKYO: SessionDefinition(
        "Asia/Tokyo", time(8), time(9), time(15, 30), time(16, 30)
    ),
    GlobalVenue.LONDON: SessionDefinition(
        "Europe/London", time(7), time(8), time(16, 30), time(17, 30)
    ),
    GlobalVenue.FRANKFURT: SessionDefinition(
        "Europe/Berlin", time(8), time(9), time(17, 30), time(18, 30)
    ),
}


@dataclass(frozen=True)
class MarketCalendar:
    holidays: dict[Market | GlobalVenue, frozenset[date]] = field(default_factory=dict)

    def state(self, market: Market | GlobalVenue, timestamp: datetime) -> MarketState:
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        venue = self._venue(market)
        session = SESSIONS[venue]
        local = timestamp.astimezone(ZoneInfo(session.timezone))
        holidays = self.holidays.get(market, self.holidays.get(venue, frozenset()))
        if local.weekday() >= 5 or local.date() in holidays:
            return MarketState.CLOSED
        local_time = local.time().replace(tzinfo=None)
        if session.pre_open <= local_time < session.regular_open:
            return MarketState.PRE_MARKET
        if session.regular_open <= local_time < session.regular_close:
            return MarketState.REGULAR_HOURS
        if session.regular_close <= local_time < session.post_close:
            return MarketState.POST_MARKET
        return MarketState.CLOSED

    def global_states(self, timestamp: datetime) -> dict[GlobalVenue, MarketState]:
        return {venue: self.state(venue, timestamp) for venue in GlobalVenue}

    @staticmethod
    def _venue(market: Market | GlobalVenue) -> GlobalVenue:
        if isinstance(market, GlobalVenue):
            return market
        if market == Market.INDIA:
            return GlobalVenue.INDIA
        if market == Market.USA:
            return GlobalVenue.USA
        raise ValueError("GLOBAL market requires an explicit GlobalVenue")
