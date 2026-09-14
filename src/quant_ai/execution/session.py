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


# NYSE full-day closures for 2026, derived from the exchange's published rules
# (fixed dates, Monday observances, and Independence Day observed on Friday 3 July
# because 4 July falls on a Saturday).
NYSE_HOLIDAYS_2026 = frozenset(
    {
        date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
        date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
        date(2026, 11, 26), date(2026, 12, 25),
    }
)

# NSE/BSE 2026 full-day closures, including the Maharashtra election closure.
# Source: https://zerodha.com/marketintel/holiday-calendar/
# Special Sunday Muhurat sessions are not enabled by this regular-session calendar.
NSE_HOLIDAYS_2026 = frozenset(
    date(2026, month, day)
    for month, day in (
        (1, 15), (1, 26), (3, 3), (3, 26), (3, 31), (4, 3), (4, 14),
        (5, 1), (5, 28), (6, 26), (9, 14), (10, 2), (10, 20),
        (11, 10), (11, 24), (12, 25),
    )
)


def default_holidays() -> dict[Market | GlobalVenue, frozenset[date]]:
    return {GlobalVenue.USA: NYSE_HOLIDAYS_2026, GlobalVenue.INDIA: NSE_HOLIDAYS_2026}


def default_special_sessions() -> dict[Market | GlobalVenue, frozenset[date]]:
    # NSE/CMTR/72349: normal 09:15–15:30 cash session on Budget Sunday.
    # https://nsearchives.nseindia.com/content/circulars/CMTR72349.pdf
    return {GlobalVenue.INDIA: frozenset({date(2026, 2, 1)})}


def holidays_from_json(
    payload: dict[str, list[str]],
    base: dict[Market | GlobalVenue, frozenset[date]] | None = None,
) -> dict[Market | GlobalVenue, frozenset[date]]:
    """Merge ``{"INDIA": ["2026-11-09", ...], "USA": [...]}`` over ``base``."""
    merged: dict[Market | GlobalVenue, frozenset[date]] = dict(base or {})
    for key, values in payload.items():
        venue = GlobalVenue(key.strip().upper())
        parsed = frozenset(date.fromisoformat(str(item)) for item in values)
        merged[venue] = merged.get(venue, frozenset()) | parsed
    return merged


@dataclass(frozen=True)
class MarketCalendar:
    holidays: dict[Market | GlobalVenue, frozenset[date]] = field(default_factory=dict)
    special_sessions: dict[Market | GlobalVenue, frozenset[date]] = field(default_factory=default_special_sessions)

    def state(self, market: Market | GlobalVenue, timestamp: datetime) -> MarketState:
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        venue = self._venue(market)
        session = SESSIONS[venue]
        local = timestamp.astimezone(ZoneInfo(session.timezone))
        holidays = self.holidays.get(market, self.holidays.get(venue, frozenset()))
        special = self.special_sessions.get(market, self.special_sessions.get(venue, frozenset()))
        if (local.weekday() >= 5 and local.date() not in special) or local.date() in holidays:
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
