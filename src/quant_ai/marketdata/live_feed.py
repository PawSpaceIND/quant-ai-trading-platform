"""Market data built entirely from the live websocket tick stream.

The ghost runtime receives real last-traded prices over Zerodha/IBKR websockets
but, until now, computed candles and marked positions from a synthetic feed
pinned to one market. This module turns the tick buffer into a proper
``MarketDataFeed``: ticks roll into closed fixed-length bars per symbol, and
the latest tick is the mark. It is market-agnostic, so any instrument the
watchlist can subscribe to - NSE equity, MCX metal, CDS rupee pair, US stock or
future - gets real candles and real marks from the same source.

A bar closes only once its full window has elapsed, so a query can never see a
bar that is still forming. That preserves the no-lookahead property the
historical replay feed already has.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import RLock
from typing import Callable

from quant_ai.domain.models import Instrument
from quant_ai.marketdata.aggregate import aggregate_windows
from quant_ai.marketdata.feed import MarketDataFeed, MarketTick
from quant_ai.marketdata.models import Candle
from quant_ai.marketdata.tick_integrity import tick_value_issue
from quant_ai.marketdata.ticker_stream import LiveTick, TickBuffer

Clock = Callable[[], datetime]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _timeframe_minutes(timeframe: str) -> int:
    raw = timeframe.strip().lower()
    if raw.endswith("m") and raw[:-1].isdigit():
        return int(raw[:-1])
    if raw.endswith("h") and raw[:-1].isdigit():
        return int(raw[:-1]) * 60
    raise ValueError(f"unsupported_timeframe:{timeframe}")


@dataclass
class _Bar:
    start: datetime
    end: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


class TickBarAggregator:
    """Rolls ticks into closed fixed-length bars, one bounded series per symbol.

    Websocket ticks carry cumulative session volume; per-bar volume is the delta
    between consecutive ticks, with a reset (new session) counted as zero.
    """

    def __init__(
        self, bar_length: timedelta = timedelta(minutes=1), max_bars: int = 2000,
        *, clock: Clock | None = None,
    ) -> None:
        if bar_length <= timedelta(0):
            raise ValueError("bar_length must be positive")
        if max_bars < 1:
            raise ValueError("max_bars must be positive")
        self.bar_length = bar_length
        self.max_bars = max_bars
        self._closed: dict[str, deque[_Bar]] = {}
        self._forming: dict[str, _Bar] = {}
        self._last_volume: dict[str, Decimal] = {}
        self._last_observed: dict[str, datetime] = {}
        self._sealed_until: dict[str, datetime] = {}
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = RLock()

    def ingest(self, tick: LiveTick) -> None:
        if tick_value_issue(tick):
            return
        observed = _utc(tick.observed_at)
        start = self._floor(observed)
        with self._lock:
            previous = self._last_observed.get(tick.symbol)
            sealed = self._sealed_until.get(tick.symbol)
            if observed > _utc(self.clock()) or (previous and observed < previous) or (sealed and observed < sealed):
                return
            delta = self._volume_delta(tick.symbol, tick.volume, observed, previous)
            self._last_observed[tick.symbol] = observed
            forming = self._forming.get(tick.symbol)
            if forming is not None and forming.start < start:
                self._close(tick.symbol)
                forming = None
            if forming is None:
                self._forming[tick.symbol] = _Bar(
                    start, start + self.bar_length, tick.ltp, tick.ltp, tick.ltp, tick.ltp, delta
                )
                return
            if observed < forming.start:
                return  # a late tick from an already-closed window never rewrites history
            forming.high = max(forming.high, tick.ltp)
            forming.low = min(forming.low, tick.ltp)
            forming.close = tick.ltp
            forming.volume += delta

    def closed_bars(
        self, symbol: str, start: datetime, end: datetime, now: datetime | None = None
    ) -> tuple[_Bar, ...]:
        """Closed bars whose end lies within ``[start, end]`` and never after ``now``."""
        current = _utc(now or datetime.now(timezone.utc))
        with self._lock:
            self._roll(symbol, current)
            series = self._closed.get(symbol, ())
            limit = min(_utc(end), current)
            lower = _utc(start)
            return tuple(bar for bar in series if lower <= bar.end <= limit)

    def latest_close(self, symbol: str, now: datetime | None = None) -> Decimal | None:
        current = _utc(now or datetime.now(timezone.utc))
        with self._lock:
            self._roll(symbol, current)
            series = self._closed.get(symbol)
            return series[-1].close if series else None

    def _roll(self, symbol: str, now: datetime) -> None:
        boundary = self._floor(now)
        self._sealed_until[symbol] = max(self._sealed_until.get(symbol, boundary), boundary)
        forming = self._forming.get(symbol)
        if forming is not None and forming.end <= now:
            self._close(symbol)

    def _close(self, symbol: str) -> None:
        bar = self._forming.pop(symbol)
        self._closed.setdefault(symbol, deque(maxlen=self.max_bars)).append(bar)

    def _floor(self, timestamp: datetime) -> datetime:
        seconds = self.bar_length.total_seconds()
        epoch = timestamp.timestamp()
        return datetime.fromtimestamp(epoch - (epoch % seconds), tz=timezone.utc)

    def _volume_delta(self, symbol: str, volume: Decimal, observed: datetime, previous: datetime | None) -> Decimal:
        last = self._last_volume.get(symbol)
        self._last_volume[symbol] = volume
        if last is None or volume < last or (previous and observed.date() != previous.date()):
            return Decimal(0)
        return volume - last


class LiveTickMarketDataFeed(MarketDataFeed):
    """``MarketDataFeed`` over the live tick buffer: real candles, real marks."""

    def __init__(
        self,
        buffer: TickBuffer,
        aggregator: TickBarAggregator | None = None,
        *,
        clock: Clock | None = None,
        max_tick_age: timedelta | None = timedelta(hours=24),
    ) -> None:
        self.buffer = buffer
        self.clock = clock or buffer.clock
        self.aggregator = aggregator or TickBarAggregator(clock=lambda: self.clock())
        self.max_tick_age = max_tick_age
        buffer.subscribe(self.aggregator.ingest)

    def fetch_ohlcv(
        self,
        instrument: Instrument,
        start: datetime,
        end: datetime,
        timeframe: str = "1m",
    ) -> tuple[Candle, ...]:
        if end <= start:
            raise ValueError("end must be after start")
        minutes = _timeframe_minutes(timeframe)
        bars = self.aggregator.closed_bars(instrument.symbol, start, end, _utc(self.clock()))
        candles = tuple(
            Candle(instrument, bar.end, bar.open, bar.high, bar.low, bar.close, bar.volume)
            for bar in bars
        )
        base = max(1, int(self.aggregator.bar_length.total_seconds() // 60))
        if minutes == base:
            return candles
        if minutes % base:
            raise ValueError(f"unsupported_timeframe:{timeframe}")
        return aggregate_windows(candles, minutes // base)

    def latest_tick(self, instrument: Instrument) -> MarketTick:
        tick = self.buffer.latest(instrument.symbol)
        if tick is None:
            raise ValueError("no_live_tick")
        observed = _utc(tick.observed_at)
        if observed > _utc(self.clock()):
            raise ValueError("future_live_tick")
        if tick_value_issue(tick):
            raise ValueError("invalid_live_tick")
        if self.max_tick_age is not None and _utc(self.clock()) - observed > self.max_tick_age:
            raise ValueError("stale_live_tick")
        bid = tick.bid if tick.bid is not None and tick.bid > 0 else tick.ltp
        ask = tick.ask if tick.ask is not None and tick.ask > 0 else tick.ltp
        if bid > ask:
            bid = ask = tick.ltp
        return MarketTick(instrument, observed, tick.ltp, bid, ask, max(Decimal(0), tick.volume))
