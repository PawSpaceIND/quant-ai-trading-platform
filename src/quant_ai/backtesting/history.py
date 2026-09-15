"""Bulk daily-bar ingestion: many years of closed sessions for the replay harness.

``DailyHistoryProvider`` reads the newest 120 sessions once per UTC day, which is the
right shape for regime context and the wrong shape for a backtest. This module is the
one-shot bulk counterpart: it walks a multi-year range in bounded chunks, keeps only
sessions that have closed, and writes the JSON that
:func:`quant_ai.backtesting.replay.load_replay_dataset` already consumes - one file per
symbol, because the harness validates one instrument per dataset.

It borrows the provider's honesty rules rather than its cache. The still-forming session
is dropped (:func:`quant_ai.marketdata.timeframes.closed_sessions`), nothing is estimated,
and a failed chunk aborts the symbol loudly instead of shortening its series.

Raw close, not adjusted close, and why
-------------------------------------
Yahoo's chart envelope carries two price series. ``indicators.quote[0]`` is the series as
it printed, and ``indicators.adjclose[0].adjclose`` is that same close back-adjusted for
every dividend paid *after* the bar's own date. A back-adjusted close is therefore a
number that did not exist on the day it is stamped with: feeding it to a backtest lets a
strategy trade on tomorrow's dividend schedule, which manufactures alpha that cannot be
earned and is almost impossible to spot in an equity curve afterwards.

``YahooFinanceMarketDataAdapter.fetch_ohlcv`` was read before this module was written: it
reads ``indicators.quote[0]`` and never touches ``adjclose``. This module reuses that
adapter unchanged, applies no adjustment of its own, and records the choice in every
payload under ``provenance.price_series`` / ``provenance.adjustment_policy`` /
``provenance.adjusted_close_used``. :func:`write_dataset` refuses to write a payload that
does not declare all three, because a dataset that is silent about its adjustment policy
is the failure this module exists to prevent.

One caveat is stated rather than hidden: Yahoo itself back-adjusts the ``quote`` series
for *splits* before serving it, and the envelope carries no un-split-adjusted series to
prefer instead. A split re-bases price and quantity together, so it does not create the
free-money leak a dividend adjustment does, but it is still information from after the
bar. ``provenance.adjustment_note`` says so in the file, so a reader never has to guess
what the numbers are.

Gaps
----
A gap is a gap. Nothing here interpolates, forward-fills or synthesises a bar. A session
the symbol did not print is reported under ``provenance.missing_sessions``, measured
against the benchmark index - the index prints on every session the exchange held, so it
is a session calendar that was observed rather than one that was assumed.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from quant_ai.domain.models import AssetClass, Instrument, Market
from quant_ai.execution.session import SESSIONS, GlobalVenue
from quant_ai.governance.directives import FounderDirectives
from quant_ai.intelligence.external.yahoo import YahooFinanceMarketDataAdapter, yahoo_symbol
from quant_ai.intelligence.resilience import (
    HttpResponse,
    HttpTransport,
    ResilientHttpClient,
    TokenBucketRateLimiter,
    UrllibTransport,
)
from quant_ai.marketdata.feed import MarketDataFeed
from quant_ai.marketdata.models import Candle
from quant_ai.marketdata.timeframes import closed_sessions, session_close_at, venue_for

LOGGER = logging.getLogger("quant_ai.bulk_history")

SOURCE = "yahoo-finance-chart-v8"
INTERVAL = "1d"

# The series this tool emits, named exactly as it appears in Yahoo's envelope so a reader
# can go and look. See the module docstring for why adjclose is refused.
PRICE_SERIES = "chart.result[0].indicators.quote[0]"
ADJUSTMENT_POLICY = "none_applied_by_this_tool"
ADJUSTED_CLOSE_USED = False
ADJUSTMENT_NOTE = (
    "Prices are Yahoo's raw quote series. indicators.adjclose[0].adjclose is never read: "
    "it back-adjusts for dividends declared after the bar's own date, which is look-ahead "
    "and manufactures alpha in a backtest. Yahoo does back-adjust the quote series for "
    "splits before serving it and offers no un-split-adjusted alternative; a split "
    "re-bases price and size together so it does not create that leak, but it is "
    "information from after the bar and is disclosed here rather than hidden."
)

# Every payload must say where it came from and what was done to the prices. write_dataset
# enforces this list, so silence about the adjustment policy cannot reach a file.
REQUIRED_PROVENANCE_KEYS = (
    "source",
    "fetched_at",
    "requested_range",
    "interval",
    "price_series",
    "adjustment_policy",
    "adjusted_close_used",
    "adjustment_note",
    "instrument",
    "benchmark",
    "bar_count",
    "first_bar",
    "last_bar",
    "chunks",
    "missing_sessions",
    "benchmark_unpaired",
)

# A decade of holidays is a long list and the payload is meant to stay readable; the count
# is always exact, the enumeration is capped and says when it was capped.
MAX_REPORTED_DATES = 250

DEFAULT_CHUNK_DAYS = 365
DEFAULT_YEARS = 10
# Yahoo is not ours to hammer: one request every two seconds by default.
DEFAULT_REQUESTS_PER_SECOND = 0.5
USER_AGENT = "pramana-bulk-history/1.0 (read-only historical bars)"

DEFAULT_DIRECTIVES_PATH = (
    Path(__file__).resolve().parents[3] / "deploy" / "founder-directives.example.json"
)


@dataclass(frozen=True)
class BenchmarkSpec:
    """The index a symbol's curve is compared against, and its Yahoo ticker."""

    name: str
    instrument: Instrument


# NIFTY 50. Yahoo's index tickers are already fully qualified, which is why yahoo_symbol
# leaves a leading-caret symbol alone instead of suffixing it to the unknown "^NSEI.NS".
NIFTY_50 = BenchmarkSpec(
    "NIFTY 50",
    Instrument("^NSEI", Market.INDIA, AssetClass.INDEX, "INR", "NSE", tradable=False),
)


class HistoryFetchError(RuntimeError):
    """A chunk, a symbol or the benchmark could not be fetched completely.

    Raised rather than returning what did arrive: a short series that looks complete is
    the outcome this whole module is arranged to prevent.
    """


class UserAgentTransport:
    """Wraps a transport so every bulk request identifies itself.

    Yahoo throttles anonymous clients hard, and an unattributed multi-year crawl is rude
    besides. The wrapper only adds a default header; anything the caller set wins.
    """

    def __init__(self, inner: HttpTransport, user_agent: str = USER_AGENT) -> None:
        self.inner = inner
        self.user_agent = user_agent

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None,
        headers: dict[str, str] | None,
        timeout_seconds: float,
        max_bytes: int,
    ) -> HttpResponse:
        merged = dict(headers or {})
        merged.setdefault("User-Agent", self.user_agent)
        return self.inner.request(
            method,
            url,
            params=params,
            headers=merged,
            timeout_seconds=timeout_seconds,
            max_bytes=max_bytes,
        )


@dataclass(frozen=True)
class ChunkReport:
    start: datetime
    end: datetime
    bars: int
    cached: bool

    def as_json(self) -> dict[str, object]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "bars": self.bars,
            "from_cache": self.cached,
        }


@dataclass(frozen=True)
class SymbolHistory:
    instrument: Instrument
    bars: tuple[Candle, ...]
    chunks: tuple[ChunkReport, ...]


def watchlist_instruments(path: str | Path | None = None) -> tuple[Instrument, ...]:
    """The pilot watchlist, read from the founder directives rather than restated here.

    The default set has to be whatever the pilot is actually configured to trade. A list
    copied into this module would keep working while silently describing a different
    portfolio the first time ``deploy/founder-directives.example.json`` changes.
    """
    source = Path(path) if path is not None else DEFAULT_DIRECTIVES_PATH
    directives = FounderDirectives.from_json(json.loads(source.read_text(encoding="utf-8")))
    if not directives.watchlist:
        raise HistoryFetchError(f"directives_have_no_watchlist:{source}")
    return directives.watchlist


def chunk_ranges(
    start: datetime, end: datetime, days: int = DEFAULT_CHUNK_DAYS
) -> tuple[tuple[datetime, datetime], ...]:
    """``end``-exclusive windows covering ``[start, end)``, oldest first.

    Ten years in one request is a large response on a bounded client and an all-or-nothing
    retry; in yearly chunks a rate limit costs one window, and the windows already
    fetched are on disk for the next run.
    """
    if end <= start:
        raise ValueError("end must be after start")
    if days < 1:
        raise ValueError("chunk days must be positive")
    windows: list[tuple[datetime, datetime]] = []
    cursor = start
    step = timedelta(days=days)
    while cursor < end:
        windows.append((cursor, min(cursor + step, end)))
        cursor += step
    return tuple(windows)


def session_date(timestamp: datetime, venue: GlobalVenue | None) -> date:
    """The local trading date a bar belongs to, for human-readable gap reports."""
    zone = ZoneInfo(SESSIONS[venue].timezone) if venue is not None else timezone.utc
    return timestamp.astimezone(zone).date()


class BulkDailyHistoryFetcher:
    """Multi-year closed daily bars for one instrument at a time.

    Source. ``YahooFinanceMarketDataAdapter.fetch_ohlcv(instrument, start, end, "1d")``
    over the ``ResilientHttpClient`` handed in, which should be this fetcher's own so a
    Yahoo rate limit opens this circuit and not the trading engine's.

    Honesty. Only sessions closed by ``now`` are kept; the live, still-forming day Yahoo
    serves alongside history is dropped. Overlapping chunks de-duplicate by session close,
    last row winning. Nothing is interpolated: a session the provider did not return is
    absent from the output and counted in the report.

    Resume. With a ``cache_dir``, each chunk's normalised rows are written there on
    success and reused on a later run, so an interrupted decade resumes where it stopped
    instead of re-requesting what already arrived. The cache holds market data; keep it
    out of the repository.
    """

    def __init__(
        self,
        client: ResilientHttpClient,
        *,
        feed: MarketDataFeed | None = None,
        chunk_days: int = DEFAULT_CHUNK_DAYS,
        cache_dir: str | Path | None = None,
    ) -> None:
        if chunk_days < 1:
            raise ValueError("chunk_days must be positive")
        self.client = client
        self.feed = feed or YahooFinanceMarketDataAdapter(client)
        self.chunk_days = chunk_days
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None

    def fetch(
        self, instrument: Instrument, start: datetime, end: datetime, now: datetime
    ) -> SymbolHistory:
        provider_symbol = yahoo_symbol(instrument.symbol, instrument.market)
        collected: list[Candle] = []
        reports: list[ChunkReport] = []
        for chunk_start, chunk_end in chunk_ranges(start, end, self.chunk_days):
            bars, cached = self._chunk(instrument, provider_symbol, chunk_start, chunk_end)
            collected.extend(bars)
            reports.append(ChunkReport(chunk_start, chunk_end, len(bars), cached))
        closed = closed_sessions(tuple(collected), now, venue_for(instrument.market))
        if not closed:
            raise HistoryFetchError(
                f"no_closed_sessions:{provider_symbol}:{start.date()}..{end.date()}"
            )
        LOGGER.info(
            "bulk history: symbol=%s provider_symbol=%s chunks=%d bars=%d first=%s last=%s",
            instrument.symbol,
            provider_symbol,
            len(reports),
            len(closed),
            closed[0].timestamp.isoformat(),
            closed[-1].timestamp.isoformat(),
        )
        return SymbolHistory(instrument, closed, tuple(reports))

    def _chunk(
        self, instrument: Instrument, provider_symbol: str, start: datetime, end: datetime
    ) -> tuple[tuple[Candle, ...], bool]:
        path = self._cache_path(provider_symbol, start, end)
        cached = self._read_cache(path, instrument, provider_symbol, start, end)
        if cached is not None:
            return cached, True
        try:
            bars = self.feed.fetch_ohlcv(instrument, start, end, INTERVAL)
        except BaseException as exc:
            # A chunk that failed is not a chunk with no sessions in it. Abort the symbol:
            # continuing would write a file that looks like a complete decade and is not.
            detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            raise HistoryFetchError(
                f"chunk_fetch_failed:{provider_symbol}:{start.date()}..{end.date()}:{detail}"
            ) from exc
        self._write_cache(path, provider_symbol, start, end, bars)
        return bars, False

    def _cache_path(self, provider_symbol: str, start: datetime, end: datetime) -> Path | None:
        if self.cache_dir is None:
            return None
        key = f"{provider_symbol}|{INTERVAL}|{start.isoformat()}|{end.isoformat()}"
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        safe = "".join(char if char.isalnum() else "-" for char in provider_symbol)
        return self.cache_dir / f"{safe}-{start:%Y%m%d}-{end:%Y%m%d}-{digest}.json"

    def _read_cache(
        self,
        path: Path | None,
        instrument: Instrument,
        provider_symbol: str,
        start: datetime,
        end: datetime,
    ) -> tuple[Candle, ...] | None:
        if path is None or not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            header = (
                payload["provider_symbol"],
                payload["interval"],
                payload["start"],
                payload["end"],
            )
            # A cache entry is only usable for the exact window it was fetched for; a
            # mismatch is treated as a miss rather than as bars for another request.
            if header != (provider_symbol, INTERVAL, start.isoformat(), end.isoformat()):
                return None
            return tuple(
                Candle(
                    instrument,
                    datetime.fromisoformat(str(row["timestamp"])),
                    Decimal(str(row["open"])),
                    Decimal(str(row["high"])),
                    Decimal(str(row["low"])),
                    Decimal(str(row["close"])),
                    Decimal(str(row["volume"])),
                )
                for row in payload["bars"]
            )
        except (OSError, ValueError, TypeError, LookupError, ArithmeticError) as exc:
            LOGGER.warning("bulk history cache unusable: path=%s reason=%s", path, exc)
            return None

    def _write_cache(
        self,
        path: Path | None,
        provider_symbol: str,
        start: datetime,
        end: datetime,
        bars: tuple[Candle, ...],
    ) -> None:
        if path is None:
            return
        payload = {
            "provider_symbol": provider_symbol,
            "interval": INTERVAL,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "price_series": PRICE_SERIES,
            "bars": [_bar_json(bar) for bar in bars],
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        except OSError as exc:
            LOGGER.warning("bulk history cache not written: path=%s reason=%s", path, exc)


def _bar_json(bar: Candle) -> dict[str, str]:
    """A bar in the shape ``replay._bar_from_mapping`` reads back.

    Decimals are emitted as strings: a float round-trip would quietly re-price a bar.
    """
    return {
        "timestamp": bar.timestamp.isoformat(),
        "open": str(bar.open),
        "high": str(bar.high),
        "low": str(bar.low),
        "close": str(bar.close),
        "volume": str(bar.volume),
    }


def _capped(values: Iterable[str]) -> dict[str, object]:
    items = list(values)
    return {
        "count": len(items),
        "dates": items[:MAX_REPORTED_DATES],
        "dates_truncated": len(items) > MAX_REPORTED_DATES,
    }


def build_payload(
    history: SymbolHistory,
    benchmark_history: SymbolHistory,
    *,
    benchmark: BenchmarkSpec = NIFTY_50,
    requested_start: datetime,
    requested_end: datetime,
    years: int,
    fetched_at: datetime,
) -> dict[str, object]:
    """The replay dataset for one symbol, with its benchmark closes aligned bar for bar.

    ``HistoricalReplayHarness`` reads ``benchmark_closes`` positionally against the equity
    curve, so entry *i* has to be the index's close on the session of bar *i*. The pairing
    is therefore an intersection of observed sessions, never a fill: a session the symbol
    printed and the index did not drops the bar and is counted under
    ``benchmark_unpaired``, and a session the index printed and the symbol did not is a
    gap in the symbol, counted under ``missing_sessions``. Carrying a stale close forward
    to keep the lengths equal would invent a benchmark print, so neither happens.
    """
    venue = venue_for(history.instrument.market)
    benchmark_closes = {
        session_close_at(bar.timestamp, venue): bar.close for bar in benchmark_history.bars
    }
    paired: list[Candle] = []
    closes: list[Decimal] = []
    unpaired: list[str] = []
    for bar in history.bars:
        close_at = session_close_at(bar.timestamp, venue)
        if close_at not in benchmark_closes:
            unpaired.append(session_date(bar.timestamp, venue).isoformat())
            continue
        paired.append(bar)
        closes.append(benchmark_closes[close_at])
    if len(paired) < 2:
        raise HistoryFetchError(
            f"insufficient_paired_sessions:{history.instrument.symbol}:{len(paired)}"
        )

    covered = {session_close_at(bar.timestamp, venue) for bar in history.bars}
    first, last = session_close_at(paired[0].timestamp, venue), session_close_at(
        paired[-1].timestamp, venue
    )
    # Sessions the exchange held - the index printed on them - that this symbol did not.
    # Bounded to the symbol's own listed span so a later listing is not read as a decade
    # of missing bars. Reported, never filled.
    missing = [
        session_date(close_at, venue).isoformat()
        for close_at in sorted(benchmark_closes)
        if first <= close_at <= last and close_at not in covered
    ]

    return {
        "bars": [_bar_json(bar) for bar in paired],
        "benchmark_closes": [str(value) for value in closes],
        "provenance": {
            "source": SOURCE,
            "fetched_at": fetched_at.isoformat(),
            "requested_range": {
                "start": requested_start.isoformat(),
                "end": requested_end.isoformat(),
                "years": years,
            },
            "interval": INTERVAL,
            "price_series": PRICE_SERIES,
            "adjustment_policy": ADJUSTMENT_POLICY,
            "adjusted_close_used": ADJUSTED_CLOSE_USED,
            "adjustment_note": ADJUSTMENT_NOTE,
            "instrument": {
                "symbol": history.instrument.symbol,
                "market": history.instrument.market.value,
                "asset_class": history.instrument.asset_class.value,
                "exchange": history.instrument.exchange,
                "currency": history.instrument.currency,
                "provider_symbol": yahoo_symbol(
                    history.instrument.symbol, history.instrument.market
                ),
            },
            "benchmark": {
                "name": benchmark.name,
                "provider_symbol": yahoo_symbol(
                    benchmark.instrument.symbol, benchmark.instrument.market
                ),
                "bar_count": len(benchmark_history.bars),
                "first_bar": benchmark_history.bars[0].timestamp.isoformat(),
                "last_bar": benchmark_history.bars[-1].timestamp.isoformat(),
            },
            "bar_count": len(paired),
            "first_bar": paired[0].timestamp.isoformat(),
            "last_bar": paired[-1].timestamp.isoformat(),
            "chunks": [report.as_json() for report in history.chunks],
            "missing_sessions": _capped(missing),
            "benchmark_unpaired": _capped(unpaired),
        },
    }


def write_dataset(path: str | Path, payload: dict[str, object]) -> Path:
    """Write a replay dataset, refusing one that does not declare its provenance.

    The refusal is the point. A file of prices with no statement of which series it holds
    and what was done to it is indistinguishable from an adjusted-close dataset, and by
    the time a backtest has been run on it nobody can tell which it was.
    """
    provenance = payload.get("provenance")
    if not isinstance(provenance, dict):
        raise TypeError("provenance_missing")
    absent = [key for key in REQUIRED_PROVENANCE_KEYS if key not in provenance]
    if absent:
        raise ValueError(f"provenance_incomplete:{','.join(absent)}")
    if not isinstance(provenance["adjusted_close_used"], bool):
        raise TypeError("adjusted_close_used must be a boolean")
    if not str(provenance["adjustment_policy"]).strip():
        raise ValueError("adjustment_policy must be stated")
    if not payload.get("bars"):
        raise ValueError("dataset has no bars")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return target


def build_datasets(
    fetcher: BulkDailyHistoryFetcher,
    instruments: tuple[Instrument, ...],
    *,
    benchmark: BenchmarkSpec = NIFTY_50,
    start: datetime,
    end: datetime,
    years: int,
    fetched_at: datetime,
) -> Iterator[tuple[Instrument, dict[str, object]]]:
    """Yield ``(instrument, payload)`` for each symbol, benchmark fetched once.

    A generator so the caller can write each dataset as it completes: a run that dies on
    the eighth symbol leaves seven finished files and a cache, not nothing.
    """
    benchmark_history = fetcher.fetch(benchmark.instrument, start, end, fetched_at)
    for instrument in instruments:
        history = fetcher.fetch(instrument, start, end, fetched_at)
        yield instrument, build_payload(
            history,
            benchmark_history,
            benchmark=benchmark,
            requested_start=start,
            requested_end=end,
            years=years,
            fetched_at=fetched_at,
        )


def default_fetcher(
    *,
    chunk_days: int = DEFAULT_CHUNK_DAYS,
    cache_dir: str | Path | None = None,
    requests_per_second: float = DEFAULT_REQUESTS_PER_SECOND,
    transport: HttpTransport | None = None,
) -> BulkDailyHistoryFetcher:
    """A fetcher on its own guarded client, so a bulk crawl cannot trip the engine's."""
    inner = transport if transport is not None else UrllibTransport()
    client = ResilientHttpClient(
        UserAgentTransport(inner),
        rate_limiter=TokenBucketRateLimiter(requests_per_second),
    )
    return BulkDailyHistoryFetcher(client, chunk_days=chunk_days, cache_dir=cache_dir)
