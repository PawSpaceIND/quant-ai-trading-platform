"""Closed higher-timeframe bars and read-only daily history for regime context.

The cadence already fetches closed 1-minute candles. ``aggregate`` rolls those into
closed N-minute bars whose buckets are anchored to the venue's regular open, so the
consensus sees the same 15-minute bars a trader's chart shows (NSE 09:15-09:30,
09:30-09:45, ...) rather than clock-aligned buckets that straddle the open.
``DailyHistoryProvider`` adds the closed daily bars an intraday tick feed cannot know,
fetched read-only at most once per instrument per UTC day and never estimated: any
failure is an empty tuple and one WARNING naming the cause.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from quant_ai.domain.models import Instrument, Market
from quant_ai.execution.session import SESSIONS, GlobalVenue
from quant_ai.intelligence.external.yahoo import YahooFinanceMarketDataAdapter
from quant_ai.intelligence.resilience import ResilientHttpClient
from quant_ai.marketdata.feed import MarketDataFeed
from quant_ai.marketdata.models import Candle

LOGGER = logging.getLogger("quant_ai.daily_history")

# Closed sessions the daily provider keeps: enough for a 40-bar regime lookback with a
# wide margin for holidays and provider gaps, small enough to stay one bounded request.
DAILY_HISTORY_SESSIONS = 120
ONE_MINUTE = timedelta(minutes=1)


def venue_for(market: Market) -> GlobalVenue | None:
    """The session venue that anchors a market's bars; ``None`` for venue-less markets."""
    if market == Market.INDIA:
        return GlobalVenue.INDIA
    if market == Market.USA:
        return GlobalVenue.USA
    return None


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _anchor(venue: GlobalVenue | None) -> tuple[ZoneInfo | timezone, time]:
    """The zone and wall-clock regular open buckets are anchored to (UTC midnight when
    the market has no venue in ``SESSIONS``)."""
    if venue is None:
        return timezone.utc, time(0)
    session = SESSIONS[venue]
    return ZoneInfo(session.timezone), session.regular_open


def _bucket(
    timestamp: datetime, minutes: int, venue: GlobalVenue | None
) -> tuple[tuple[date, int], datetime]:
    """The ``(local date, index)`` bucket of a close-stamped candle and that bucket's close.

    Bucket ``k`` of a local date covers ``(open + k*N, open + (k+1)*N]`` in wall-clock
    time, so a candle closing exactly on a boundary belongs to the bucket it closes.
    Negative indexes are pre-open buckets, still aligned to the open. The local date is
    taken from just inside the candle rather than from its closing instant, so a candle
    closing exactly at local midnight stays in the day it traded in.
    """
    zone, open_time = _anchor(venue)
    local = _utc(timestamp).astimezone(zone)
    interior = local - timedelta(microseconds=1)
    anchor = datetime.combine(interior.date(), open_time, tzinfo=zone)
    offset = local - anchor
    whole = offset // ONE_MINUTE
    elapsed = whole + (1 if offset - whole * ONE_MINUTE > timedelta(0) else 0)
    index = (elapsed - 1) // minutes
    close_at = anchor + timedelta(minutes=(index + 1) * minutes)
    return (interior.date(), index), close_at.astimezone(timezone.utc)


def aggregate(
    candles: tuple[Candle, ...],
    minutes: int,
    *,
    venue: GlobalVenue | None = None,
    now: datetime | None = None,
) -> tuple[Candle, ...]:
    """Closed ``minutes``-minute bars from 1-minute candles stamped at their close.

    Buckets are anchored to the venue's regular open on each local trading date (NSE
    15-minute bars run 09:15-09:30, 09:30-09:45, ...; a 60-minute NYSE bar runs
    09:30-10:30), so a bar never spans two sessions. Input candles are stamped at their
    close, as ``LiveTickMarketDataFeed`` produces them, and every output bar is stamped at
    its close too. Gaps are tolerated: a bucket with missing minutes still closes once
    its window has elapsed. The trailing bucket is kept only when it is known to be
    closed - its final minute candle is present, or ``now`` is at or past its close -
    so a forming bar is never reported as a closed one. ``venue`` defaults to the
    instrument's market; markets without a venue anchor to UTC midnight.
    """
    if minutes < 1:
        raise ValueError("minutes must be positive")
    if not candles:
        return ()
    instrument = candles[0].instrument
    if any(candle.instrument != instrument for candle in candles):
        raise ValueError("all candles must share one instrument")
    if venue is None:
        venue = venue_for(instrument.market)
    members: dict[tuple[date, int], list[Candle]] = {}
    closes: dict[tuple[date, int], datetime] = {}
    for candle in sorted(candles, key=lambda item: _utc(item.timestamp)):
        key, close_at = _bucket(candle.timestamp, minutes, venue)
        members.setdefault(key, []).append(candle)
        closes[key] = close_at
    keys = list(members)
    limit = _utc(now) if now is not None else None
    bars: list[Candle] = []
    for position, key in enumerate(keys):
        bucket = members[key]
        close_at = closes[key]
        if position == len(keys) - 1:
            final_present = _utc(bucket[-1].timestamp) == close_at
            elapsed = limit is not None and limit >= close_at
            if not (final_present or elapsed):
                break
        bars.append(
            Candle(
                instrument,
                close_at,
                bucket[0].open,
                max(item.high for item in bucket),
                min(item.low for item in bucket),
                bucket[-1].close,
                sum((item.volume for item in bucket), Decimal(0)),
            )
        )
    return tuple(bars)


def session_close_at(timestamp: datetime, venue: GlobalVenue | None) -> datetime:
    """When the regular session containing ``timestamp`` closes, in UTC.

    Venue-less markets have no session; their day closes at the next UTC midnight.
    """
    if venue is None:
        day = _utc(timestamp).date()
        return datetime.combine(day + timedelta(days=1), time(0), tzinfo=timezone.utc)
    session = SESSIONS[venue]
    zone = ZoneInfo(session.timezone)
    local = _utc(timestamp).astimezone(zone)
    return datetime.combine(local.date(), session.regular_close, tzinfo=zone).astimezone(
        timezone.utc
    )


def opens_the_regular_session(bar: Candle, venue: GlobalVenue | None) -> bool:
    """Whether a session-open-stamped bar starts at the venue's regular open.

    Providers stamp a daily bar at the moment its session began, so this separates the
    regular session from a special one held on the same date. A venue-less market has no
    session to be regular, and every bar counts as one.
    """
    if venue is None:
        return True
    session = SESSIONS[venue]
    local = _utc(bar.timestamp).astimezone(ZoneInfo(session.timezone))
    return local.timetz().replace(tzinfo=None) == session.regular_open


def session_date(timestamp: datetime, venue: GlobalVenue | None) -> date:
    """The local trading date a bar belongs to.

    A daily bar is stamped in UTC but belongs to a session held in the venue's own day, and
    which day that is decides whether two bars are one session or two. Venue-less markets
    have no local day; theirs is the UTC one.
    """
    zone = ZoneInfo(SESSIONS[venue].timezone) if venue is not None else timezone.utc
    return _utc(timestamp).astimezone(zone).date()


def closed_sessions(
    bars: tuple[Candle, ...], now: datetime, venue: GlobalVenue | None
) -> tuple[Candle, ...]:
    """Daily bars whose session has closed by ``now``, one per session, oldest first.

    A provider that serves the live day alongside history reports a still-forming bar
    for it; that bar is dropped (the intraday bars cover the current session).

    Two bars can land on one session close, and the two reasons are not the same thing.
    A provider may *revise* a session and send the row again, and there the later row is
    the correction. Or the exchange may have held a **second, special session** on that
    date - NSE's one-hour Diwali Muhurat sitting at 18:15 IST, hours after the regular
    09:15-15:30 session - which the provider stamps at its own start. Taking the later row
    there substitutes a ceremonial hour's open, high, low and close for the whole trading
    day's, and nothing downstream can tell: the count is unchanged, the date is right, and
    only the prices are wrong.

    So the regular session wins over a special one on the same date, and among bars of
    equal standing the later row still wins, which keeps the revision case intact.
    """
    limit = _utc(now)
    by_close: dict[datetime, Candle] = {}
    for bar in sorted(bars, key=lambda item: _utc(item.timestamp)):
        close_at = session_close_at(bar.timestamp, venue)
        if close_at > limit:
            continue
        held = by_close.get(close_at)
        if (
            held is None
            or opens_the_regular_session(bar, venue)
            or not opens_the_regular_session(held, venue)
        ):
            by_close[close_at] = bar
    return tuple(by_close.values())


class DailyHistoryProvider:
    """Read-only closed daily bars for regime context.

    Source. ``YahooFinanceMarketDataAdapter.fetch_ohlcv(instrument, start, now, "1d")``
    through the ``ResilientHttpClient`` handed in, which should be this provider's own so
    a Yahoo rate-limit opens this circuit and not the news or macro one. Construction
    performs no I/O; the first ``fetch`` does.

    Honesty. Only sessions that have closed by ``now`` are returned (Yahoo serves the
    live, still-forming day too; it is dropped and the intraday bars cover it), bounded
    to the newest ``sessions`` bars. Any HTTP failure, envelope error, malformed row or
    arithmetic error abstains: the result is ``()`` and one WARNING names the cause.
    Nothing is interpolated or estimated.

    Cache. Daily bars change once a session, so each instrument is fetched at most once
    per UTC day and held in memory, abstentions included; the next UTC day retries. The
    cache is per process; a restart refetches.
    """

    provider_id = "yahoo-daily-history"

    def __init__(
        self,
        client: ResilientHttpClient,
        *,
        sessions: int = DAILY_HISTORY_SESSIONS,
        feed: MarketDataFeed | None = None,
    ) -> None:
        if sessions < 1:
            raise ValueError("sessions must be positive")
        self.client = client
        self.sessions = sessions
        self.feed = feed or YahooFinanceMarketDataAdapter(client)
        self._cache: dict[tuple[str, str], tuple[date, tuple[Candle, ...]]] = {}

    @property
    def calendar_days(self) -> int:
        """Calendar span requested: five sessions a week plus a three-week holiday margin."""
        return self.sessions * 7 // 5 + 21

    def fetch(self, instrument: Instrument, now: datetime) -> tuple[Candle, ...]:
        current = _utc(now)
        key = (instrument.symbol.upper(), instrument.market.value)
        cached = self._cache.get(key)
        if cached is not None and cached[0] == current.date():
            return cached[1]
        bars = self._fetch_uncached(instrument, current)
        self._cache[key] = (current.date(), bars)
        return bars

    def cached(self, instrument: Instrument, now: datetime) -> tuple[Candle, ...]:
        """No I/O: observation for this UTC day, or an explicitly empty cache."""
        current = _utc(now)
        cached = self._cache.get((instrument.symbol.upper(), instrument.market.value))
        return cached[1] if cached is not None and cached[0] == current.date() else ()

    def _fetch_uncached(self, instrument: Instrument, now: datetime) -> tuple[Candle, ...]:
        start = now - timedelta(days=self.calendar_days)
        try:
            raw = self.feed.fetch_ohlcv(instrument, start, now, "1d")
            closed = closed_sessions(raw, now, venue_for(instrument.market))
        except (
            TimeoutError,
            OSError,
            RuntimeError,
            ValueError,
            TypeError,
            LookupError,
            AttributeError,
            ArithmeticError,
        ) as exc:
            detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            LOGGER.warning(
                "daily history abstain: symbol=%s reason=%s", instrument.symbol, detail
            )
            return ()
        return closed[-self.sessions :]
