from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
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
    # MCX runs an evening session that tracks COMEX, so its close moves with *US*
    # daylight saving: 23:30 IST while New York is on DST, 23:55 IST otherwise. Both
    # variants are stated outright rather than derived by adding 25 minutes to a base,
    # because that arithmetic silently crosses midnight and every time comparison in
    # ``state`` below assumes a session that begins and ends on the same local day.
    us_dst_regular_close: time | None = None
    us_dst_post_close: time | None = None

    def closes_on(self, day: date) -> tuple[time, time]:
        """The ``(regular_close, post_close)`` in force on ``day``."""
        if self.us_dst_regular_close is None:
            return self.regular_close, self.post_close
        noon = datetime.combine(day, time(12), tzinfo=ZoneInfo("America/New_York"))
        if not noon.dst():
            return self.regular_close, self.post_close
        return self.us_dst_regular_close, self.us_dst_post_close or self.us_dst_regular_close


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


# India trades one country across several exchanges that do not keep the same hours, so a
# single "INDIA" session is wrong for everything but NSE/BSE cash. An MCX metal is live for
# eight hours after the equity market shuts; treating it as closed is not a conservative
# error, it is eight hours of an open position that nothing is deciding about.
#
# Hours below are the regular continuous sessions as published by each exchange. Half-days,
# Muhurat sessions and commodity-specific agri timings are not encoded: an operator who
# needs one supplies it, rather than the platform guessing.
INDIA_EXCHANGE_SESSIONS: dict[str, SessionDefinition] = {
    # Cash equity and equity derivatives.
    "NSE": SessionDefinition("Asia/Kolkata", time(9), time(9, 15), time(15, 30), time(16)),
    "BSE": SessionDefinition("Asia/Kolkata", time(9), time(9, 15), time(15, 30), time(16)),
    "NFO": SessionDefinition("Asia/Kolkata", time(9), time(9, 15), time(15, 30), time(16)),
    "BFO": SessionDefinition("Asia/Kolkata", time(9), time(9, 15), time(15, 30), time(16)),
    # Currency derivatives close at 17:00 IST.
    "CDS": SessionDefinition("Asia/Kolkata", time(9), time(9), time(17), time(17, 30)),
    "BCD": SessionDefinition("Asia/Kolkata", time(9), time(9), time(17), time(17, 30)),
    # Non-agri commodities run 09:00 through the evening session. There is no post-market
    # window, so ``post_close`` equals the close and the state goes straight to CLOSED.
    # MCX Trade Timings: 23:30, extended to23:55 typically November–March.
    # https://www.mcxindia.com/market-operations/trading-surveillance
    "MCX": SessionDefinition(
        "Asia/Kolkata", time(8, 45), time(9), time(23, 55), time(23, 55),
        us_dst_regular_close=time(23, 30), us_dst_post_close=time(23, 30),
    ),
    # Agri commodities close in the evening rather than at night.
    "NCDEX": SessionDefinition("Asia/Kolkata", time(9), time(9), time(17), time(17, 30)),
}


def session_for(
    market: Market | GlobalVenue, exchange: str | None = None
) -> SessionDefinition:
    """The session governing ``market``, narrowed by ``exchange`` where one is known.

    An unrecognised exchange falls back to the venue session rather than raising: a new
    or mis-spelled code must not take the engine down, and the venue session is the
    conservative answer (it is the shortest of the Indian ones).
    """
    code = (exchange or "").strip().upper()
    venue = venue_of(market)
    if venue == GlobalVenue.INDIA and code in INDIA_EXCHANGE_SESSIONS:
        return INDIA_EXCHANGE_SESSIONS[code]
    return SESSIONS[venue]


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


def venue_of(market: Market | GlobalVenue) -> GlobalVenue:
    """The venue whose session definition governs a market."""
    if isinstance(market, GlobalVenue):
        return market
    if market == Market.INDIA:
        return GlobalVenue.INDIA
    if market == Market.USA:
        return GlobalVenue.USA
    raise ValueError("GLOBAL market requires an explicit GlobalVenue")


def regular_session_length(
    market: Market | GlobalVenue, exchange: str | None = None
) -> timedelta:
    """Length of one regular trading session, used to annualise intraday statistics.

    The MCX variant is measured against its standard-time 23:55 close. The DST
    session closes 25 minutes earlier. This is a fixed normal-session convention,
    not a reconstruction of actual sessions or holidays in a sampled dataset.
    Session-specific return statistics must retain their actual sampling metadata.
    """
    session = session_for(market, exchange)
    opened = datetime.combine(date(2000, 1, 1), session.regular_open)
    closed = datetime.combine(date(2000, 1, 1), session.regular_close)
    return closed - opened


def intraday_periods_per_year(
    market: Market | GlobalVenue, interval: timedelta, exchange: str | None = None
) -> int:
    """Sampling intervals in a trading year for an intraday series on ``market``.

    Zero when the market has no session definition to annualise against, which makes
    every annualised ratio downstream report nothing rather than a daily-scaled guess.
    """
    from quant_ai.analytics.metrics import annualisation_periods

    try:
        return annualisation_periods(interval, regular_session_length(market, exchange))
    except (KeyError, ValueError):
        return 0


def default_holidays() -> dict[Market | GlobalVenue, frozenset[date]]:
    return {GlobalVenue.USA: NYSE_HOLIDAYS_2026, GlobalVenue.INDIA: NSE_HOLIDAYS_2026}


def default_special_sessions() -> dict[Market | GlobalVenue, frozenset[date]]:
    # NSE/CMTR/72349: normal 09:15–15:30 cash session on Budget Sunday.
    # https://nsearchives.nseindia.com/content/circulars/CMTR72349.pdf
    return {GlobalVenue.INDIA: frozenset({date(2026, 2, 1)})}


def holidays_from_json(
    payload: dict[str, list[str]],
    base: dict[Market | GlobalVenue | str, frozenset[date]] | None = None,
) -> dict[Market | GlobalVenue | str, frozenset[date]]:
    """Merge ``{"INDIA": [...], "MCX": [...]}`` over ``base``.

    Keys are venues or India exchange codes. An exchange key replaces the venue list for
    that exchange alone, which is how an operator states the days MCX keeps and NSE does
    not without editing the national calendar every other venue reads.
    """
    if not isinstance(payload, dict):
        raise TypeError("Holiday overrides must be a venue-to-date-list object")
    merged: dict[Market | GlobalVenue | str, frozenset[date]] = dict(base or {})
    for key, values in payload.items():
        if not isinstance(key, str) or not isinstance(values, list) or any(not isinstance(item, str) for item in values):
            raise TypeError("Holiday overrides require string venues and lists of ISO date strings")
        name = key.strip().upper()
        target: GlobalVenue | str = name if name in INDIA_EXCHANGE_SESSIONS else GlobalVenue(name)
        parsed = frozenset(date.fromisoformat(item) for item in values)
        merged[target] = merged.get(target, frozenset()) | parsed
    return merged


@dataclass(frozen=True)
class MarketCalendar:
    """Session state per venue, narrowed to the exchange an instrument actually trades on.

    ``exchanges`` maps symbol to exchange code, so a caller holding only an order - which
    carries no exchange - still resolves the right session. The daemon builds it from the
    founder watchlist, which is where the operator already stated the venue of every
    instrument. A symbol absent from the map falls back to the venue session, so an
    unmapped instrument is judged by the shortest Indian session rather than the longest.
    """

    holidays: dict[Market | GlobalVenue | str, frozenset[date]] = field(default_factory=dict)
    special_sessions: dict[Market | GlobalVenue | str, frozenset[date]] = field(default_factory=default_special_sessions)
    exchanges: dict[str, str] = field(default_factory=dict)

    def state(
        self,
        market: Market | GlobalVenue,
        timestamp: datetime,
        *,
        exchange: str | None = None,
        symbol: str | None = None,
    ) -> MarketState:
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        venue = self._venue(market)
        code = self.exchange_for(exchange=exchange, symbol=symbol)
        session = session_for(market, code)
        local = timestamp.astimezone(ZoneInfo(session.timezone))
        holidays = self._dates(self.holidays, market, venue, code)
        special = self._dates(self.special_sessions, market, venue, code)
        if (local.weekday() >= 5 and local.date() not in special) or local.date() in holidays:
            return MarketState.CLOSED
        local_time = local.time().replace(tzinfo=None)
        regular_close, post_close = session.closes_on(local.date())
        if session.pre_open <= local_time < session.regular_open:
            return MarketState.PRE_MARKET
        if session.regular_open <= local_time < regular_close:
            return MarketState.REGULAR_HOURS
        if regular_close <= local_time < post_close:
            return MarketState.POST_MARKET
        return MarketState.CLOSED

    def exchange_for(self, *, exchange: str | None = None, symbol: str | None = None) -> str:
        """The exchange code to judge by: the explicit one, else the symbol's mapping."""
        if exchange:
            return exchange.strip().upper()
        return self.exchanges.get((symbol or "").strip().upper(), "")

    def session(
        self, market: Market | GlobalVenue, *, exchange: str | None = None, symbol: str | None = None
    ) -> SessionDefinition:
        """The session this calendar would judge such an instrument by."""
        return session_for(market, self.exchange_for(exchange=exchange, symbol=symbol))

    def global_states(self, timestamp: datetime) -> dict[GlobalVenue, MarketState]:
        return {venue: self.state(venue, timestamp) for venue in GlobalVenue}

    @staticmethod
    def _dates(
        source: dict[Market | GlobalVenue | str, frozenset[date]],
        market: Market | GlobalVenue,
        venue: GlobalVenue,
        code: str,
    ) -> frozenset[date]:
        """Exchange override first, then the market, then the venue.

        An exchange with no entry of its own inherits the venue list rather than trading
        through a national holiday. MCX keeps its own calendar and is open on a handful of
        days NSE is not; inheriting costs those sessions, while the opposite mistake would
        have the engine deciding into a closed book. An operator who needs the difference
        supplies an ``MCX`` key - see :func:`holidays_from_json`.
        """
        for key in (code, market, venue):
            if key and key in source:
                return source[key]
        return frozenset()

    @staticmethod
    def _venue(market: Market | GlobalVenue) -> GlobalVenue:
        return venue_of(market)
