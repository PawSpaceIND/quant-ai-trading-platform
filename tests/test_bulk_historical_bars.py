"""The bulk ingest that has to be boring, because a backtest cannot audit its own input.

Everything downstream of this module - the replay harness, the tearsheet, the decision to
put capital behind a strategy - trusts a file of prices without being able to check it.
Three ways that file can lie, and each has a test here that goes red when its guard is
removed.

The first is the adjusted close. Yahoo serves ``adjclose`` next to ``close``, and
``adjclose`` is the same bar back-adjusted for every dividend paid after it. Swap one for
the other and a strategy is trading on a dividend schedule that had not been announced
yet: the equity curve goes up, the leak is invisible, and nothing in this repository can
find it afterwards. So the tests pin the series written (the raw quote), and pin that a
file is *refused* unless it says in its own provenance which series it holds.

The second is the filled gap. A session with no print is one bar of information - the
symbol did not trade - and interpolating it replaces that with a number nobody quoted.
Missing sessions here are counted and named, never manufactured, and the benchmark series
is paired session by session rather than carried forward to make the lengths match.

The third is the short series that looks complete. Ten years is sixty-odd requests and
some of them will fail; a run that swallows a failed chunk writes a file with a year
missing from the middle and a bar count that still looks plausible. A failed chunk
therefore aborts that symbol, leaves no file for it, and keeps the windows that did arrive
in a cache so the rerun is cheap.

No test here reaches the network. Every chart payload is canned.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from quant_ai.backtesting.history import (
    ADJUSTED_CLOSE_USED,
    NIFTY_50,
    PRICE_SERIES,
    REQUIRED_PROVENANCE_KEYS,
    USER_AGENT,
    BulkDailyHistoryFetcher,
    HistoryFetchError,
    UserAgentTransport,
    build_datasets,
    build_payload,
    chunk_ranges,
    default_fetcher,
    watchlist_instruments,
    write_dataset,
)
from quant_ai.backtesting.replay import (
    HistoricalReplayHarness,
    dataset_instrument,
    load_replay_dataset,
)
from quant_ai.domain.models import AssetClass, Instrument, Market
from quant_ai.intelligence.external.yahoo import yahoo_symbol
from quant_ai.intelligence.resilience import (
    HttpResponse,
    ResilientHttpClient,
    TokenBucketRateLimiter,
)

ROOT = Path(__file__).resolve().parents[1]
IST = ZoneInfo("Asia/Kolkata")
INFY = Instrument("INFY", Market.INDIA, AssetClass.EQUITY, "INR", "NSE")
TCS = Instrument("TCS", Market.INDIA, AssetClass.EQUITY, "INR", "NSE")

# A Monday. Four hundred weekdays is long enough that the range has to be chunked and
# short enough that the canned payloads stay readable.
FIRST_SESSION = date(2023, 1, 2)
SESSION_COUNT = 400
CHUNK_DAYS = 200

# What a dataset has to say about itself before it may be written. Named here, in the
# test, so that removing one from the module's own list is a failure rather than a
# quietly narrower check.
MUST_DECLARE = (
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

_spec = importlib.util.spec_from_file_location(
    "fetch_historical_bars", ROOT / "scripts/fetch_historical_bars.py"
)
CLI = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(CLI)


def session_opens(count: int = SESSION_COUNT, *, start: date = FIRST_SESSION) -> list[datetime]:
    """NSE opens (09:15 IST) on ``count`` consecutive weekdays, as UTC instants.

    Yahoo stamps a daily bar at the session open, which is what ``closed_sessions`` reads
    to decide whether the session has finished.
    """
    opens: list[datetime] = []
    day = start
    while len(opens) < count:
        if day.weekday() < 5:
            opens.append(datetime.combine(day, time(9, 15), tzinfo=IST).astimezone(timezone.utc))
        day += timedelta(days=1)
    return opens


def span(opens: list[datetime]) -> tuple[datetime, datetime, datetime]:
    """``(start, end, now)`` covering every session in ``opens``, all of them closed."""
    start = opens[0] - timedelta(days=1)
    end = opens[-1] + timedelta(days=2)
    return start, end, end


def chunks_per_symbol(opens: list[datetime], days: int = CHUNK_DAYS) -> int:
    start, end, _ = span(opens)
    return len(chunk_ranges(start, end, days))


def local_dates(stamps) -> list[str]:
    return sorted(stamped.astimezone(IST).date().isoformat() for stamped in stamps)


class ChartServer:
    """Serves Yahoo v8 chart daily envelopes from an in-memory book. No network.

    Each request is answered with only the sessions inside its own ``period1``/``period2``
    window, so chunking, overlap and resume behave the way they would against Yahoo.

    ``fail_windows`` names windows by the order they are first asked for - the benchmark's
    come before the first symbol's - and a named window fails every time it is asked for,
    retries included, the way an unreachable provider does.
    """

    def __init__(self) -> None:
        self.books: dict[str, dict[datetime, dict[str, float | None]]] = {}
        self.requests: list[tuple[str, dict[str, str], dict[str, str] | None]] = []
        self.windows: list[tuple[str, str, str]] = []
        self.fail_windows: set[int] = set()

    def add(
        self,
        provider_symbol: str,
        opens: list[datetime],
        *,
        base: float = 100.0,
        blank: frozenset[datetime] = frozenset(),
        skip: frozenset[datetime] = frozenset(),
    ) -> None:
        """``blank`` sessions come back as Yahoo nulls; ``skip`` sessions are absent."""
        book: dict[datetime, dict[str, float | None]] = {}
        for index, opened in enumerate(opens):
            if opened in skip:
                continue
            if opened in blank:
                book[opened] = dict.fromkeys(("open", "high", "low", "close", "volume"))
                continue
            price = base + index
            book[opened] = {
                "open": price,
                "high": price + 1,
                "low": price - 1,
                "close": price,
                "volume": 1000 + index,
            }
        self.books[provider_symbol] = book

    def request(self, method, url, *, params, headers, timeout_seconds, max_bytes):
        provider_symbol = url.rsplit("/", 1)[-1]
        self.requests.append((provider_symbol, dict(params or {}), headers))
        window = (provider_symbol, params["period1"], params["period2"])
        if window not in self.windows:
            self.windows.append(window)
        if self.windows.index(window) in self.fail_windows:
            raise OSError("provider_unreachable")
        book = self.books.get(provider_symbol)
        if book is None:
            return HttpResponse(404, b'{"chart":{"result":null,"error":"not_found"}}', {})
        low, high = int(params["period1"]), int(params["period2"])
        rows = [
            (opened, values)
            for opened, values in sorted(book.items())
            if low <= int(opened.timestamp()) < high
        ]
        quote = {
            name: [values[name] for _, values in rows]
            for name in ("open", "high", "low", "close", "volume")
        }
        # Yahoo ships the back-adjusted close alongside the raw one. Here it is exactly
        # half the raw close, so a reader that took the wrong series is unmistakable.
        adjusted = [None if values["close"] is None else values["close"] / 2 for _, values in rows]
        payload = {
            "chart": {
                "result": [
                    {
                        "timestamp": [int(opened.timestamp()) for opened, _ in rows],
                        "indicators": {"quote": [quote], "adjclose": [{"adjclose": adjusted}]},
                    }
                ],
                "error": None,
            }
        }
        return HttpResponse(200, json.dumps(payload).encode(), {})


def bulk_fetcher(server: ChartServer, **kwargs) -> BulkDailyHistoryFetcher:
    client = ResilientHttpClient(
        server,
        rate_limiter=TokenBucketRateLimiter(10.0, 1.0),
        sleeper=lambda _seconds: None,
    )
    kwargs.setdefault("chunk_days", CHUNK_DAYS)
    return BulkDailyHistoryFetcher(client, **kwargs)


def equity_and_index(**symbol_kwargs) -> tuple[ChartServer, list[datetime]]:
    """One NSE equity and the NIFTY index over the same weekday sessions."""
    opens = session_opens()
    server = ChartServer()
    server.add("INFY.NS", opens, base=1000.0, **symbol_kwargs)
    server.add("^NSEI", opens, base=18000.0)
    return server, opens


def one_dataset(server: ChartServer, opens: list[datetime], **kwargs) -> dict:
    start, end, now = span(opens)
    built = list(
        build_datasets(
            bulk_fetcher(server, **kwargs),
            (INFY,),
            start=start,
            end=end,
            years=2,
            fetched_at=now,
        )
    )
    assert len(built) == 1
    return built[0][1]


def cli_server() -> tuple[ChartServer, list[datetime]]:
    opens = session_opens()
    server = ChartServer()
    server.add("^NSEI", opens, base=18000.0)
    server.add("INFY.NS", opens, base=1000.0)
    server.add("TCS.NS", opens, base=3000.0)
    return server, opens


LIVE_FETCHER = CLI.default_fetcher


def run_cli(server: ChartServer, opens: list[datetime], out: Path, chunk_days: int) -> int:
    """Drive the real script end to end, with only the HTTP transport swapped out."""
    CLI.default_fetcher = lambda **kwargs: bulk_fetcher(
        server, chunk_days=kwargs["chunk_days"], cache_dir=kwargs["cache_dir"]
    )
    try:
        return CLI.main([
            "--symbols", "INFY", "TCS",
            "--years", "2",
            "--end", (opens[-1] + timedelta(days=2)).isoformat(),
            "--out-dir", str(out),
            "--chunk-days", str(chunk_days),
        ])
    finally:
        CLI.default_fetcher = LIVE_FETCHER


# ------------------------------------------------------- the adjusted-close trap


def test_the_close_written_is_the_printed_close_and_never_yahoos_adjusted_close() -> None:
    """The look-ahead leak this module exists to refuse.

    ``adjclose`` back-adjusts a bar for dividends declared after its own date. A backtest
    fed those closes trades on information that did not exist yet and shows alpha nobody
    could have earned. The canned server serves an ``adjclose`` at exactly half the raw
    close, so if any bar here were adjusted the arithmetic would say so immediately.
    """
    server, opens = equity_and_index()
    payload = one_dataset(server, opens)

    for bar in payload["bars"]:
        index = opens.index(datetime.fromisoformat(bar["timestamp"]))
        assert Decimal(bar["close"]) == Decimal(str(1000.0 + index))
        assert Decimal(bar["close"]) != Decimal(str((1000.0 + index) / 2))
    assert Decimal(payload["benchmark_closes"][0]) == Decimal(18000)  # not 9000

    provenance = payload["provenance"]
    assert provenance["adjusted_close_used"] is False is ADJUSTED_CLOSE_USED
    assert provenance["price_series"] == PRICE_SERIES == "chart.result[0].indicators.quote[0]"
    assert "adjclose" in provenance["adjustment_note"]


def test_a_dataset_that_does_not_declare_its_adjustment_policy_cannot_be_written(
    tmp_path: Path,
) -> None:
    """Silence about the price series is the failure mode, so the writer refuses it.

    A file of numbers with no statement of which series it holds is indistinguishable from
    an adjusted-close file, and once a backtest has been run on it nobody can tell which it
    was. Every provenance key is load-bearing: dropping any one of them is a refusal to
    write, not a warning beside a file that got written anyway.

    The keys are spelled out here rather than read from ``REQUIRED_PROVENANCE_KEYS``. A
    test that iterates the constant it is meant to pin stops checking a key the moment
    somebody deletes it from the constant, which is the deletion that matters.
    """
    server, opens = equity_and_index()
    payload = one_dataset(server, opens)
    assert write_dataset(tmp_path / "INFY.json", payload).exists()
    assert set(MUST_DECLARE) <= set(REQUIRED_PROVENANCE_KEYS)

    for key in MUST_DECLARE:
        maimed = json.loads(json.dumps(payload))
        maimed["provenance"].pop(key)
        with pytest.raises(ValueError, match=f"provenance_incomplete:.*{key}"):
            write_dataset(tmp_path / "refused.json", maimed)
        assert not (tmp_path / "refused.json").exists()

    blank_policy = json.loads(json.dumps(payload))
    blank_policy["provenance"]["adjustment_policy"] = "   "
    with pytest.raises(ValueError, match="adjustment_policy must be stated"):
        write_dataset(tmp_path / "refused.json", blank_policy)

    evasive = json.loads(json.dumps(payload))
    evasive["provenance"]["adjusted_close_used"] = "maybe"
    with pytest.raises(TypeError, match="adjusted_close_used"):
        write_dataset(tmp_path / "refused.json", evasive)


# ------------------------------------------------------------- a gap is a gap


def test_a_session_the_symbol_did_not_print_is_counted_and_named_never_filled() -> None:
    """An absent print is information; a filled one is a number nobody quoted.

    Yahoo reports a session with no trade as nulls in the quote arrays. Those sessions must
    leave the series shorter, not carry the previous close forward - a forward-filled bar
    reads as a day of zero return on a day of real liquidity, and a strategy will trade on
    both. The index prints on every session the exchange held, so it supplies the calendar
    the gaps are measured against without one having to be assumed.
    """
    opens = session_opens()
    missing = frozenset(opens[index] for index in (50, 51, 120))
    server, _ = equity_and_index(blank=missing)
    payload = one_dataset(server, opens)

    stamps = {datetime.fromisoformat(bar["timestamp"]) for bar in payload["bars"]}
    assert stamps.isdisjoint(missing)
    assert len(payload["bars"]) == len(opens) - len(missing)

    report = payload["provenance"]["missing_sessions"]
    assert report["count"] == 3
    assert report["dates"] == local_dates(missing)
    assert report["dates_truncated"] is False
    # And nothing was invented in their place: every close is still its own session's.
    for bar in payload["bars"]:
        index = opens.index(datetime.fromisoformat(bar["timestamp"]))
        assert Decimal(bar["close"]) == Decimal(str(1000.0 + index))


def test_a_benchmark_close_is_never_carried_forward_to_keep_the_lengths_equal() -> None:
    """``benchmark_closes`` is read positionally against the equity curve.

    ``HistoricalReplayHarness`` pairs entry *i* with bar *i*, so a benchmark series padded
    out to the right length compares a strategy against an index print that never happened.
    A session the symbol traded and the index did not therefore drops the bar and says so,
    rather than repeating yesterday's index close to keep the two arrays the same size.
    """
    opens = session_opens()
    server = ChartServer()
    server.add("INFY.NS", opens, base=1000.0)
    absent = frozenset(opens[index] for index in (70, 200))
    server.add("^NSEI", opens, base=18000.0, skip=absent)
    payload = one_dataset(server, opens)

    stamps = [datetime.fromisoformat(bar["timestamp"]) for bar in payload["bars"]]
    assert set(stamps).isdisjoint(absent)
    unpaired = payload["provenance"]["benchmark_unpaired"]
    assert unpaired["count"] == 2
    assert unpaired["dates"] == local_dates(absent)

    closes = [Decimal(value) for value in payload["benchmark_closes"]]
    assert len(closes) == len(stamps) == len(opens) - 2
    for stamped, close in zip(stamps, closes):
        assert close == Decimal(str(18000.0 + opens.index(stamped)))
    assert len(set(closes)) == len(closes)  # no repeated close, so nothing was held over


# ----------------------------------------- a failed chunk is not an empty chunk


def test_a_failed_chunk_aborts_the_symbol_rather_than_shortening_its_series(
    tmp_path: Path,
) -> None:
    """A run that swallows a failed window writes a decade with a year missing from it.

    The bar count still looks plausible and the file still loads, so the hole surfaces much
    later as a strategy that mysteriously does nothing for twelve months. The fetch raises
    instead, naming the symbol and the window, and nothing is written for that symbol.
    """
    server, opens = equity_and_index()
    start, end, now = span(opens)
    # The benchmark is fetched first, so the symbol's own first window is the next request.
    server.fail_windows = {chunks_per_symbol(opens)}

    with pytest.raises(HistoryFetchError, match="chunk_fetch_failed:INFY.NS"):
        list(
            build_datasets(
                bulk_fetcher(server, cache_dir=tmp_path / "cache"),
                (INFY,),
                start=start,
                end=end,
                years=2,
                fetched_at=now,
            )
        )
    assert not list(tmp_path.glob("*.json"))


def test_a_ticker_the_provider_does_not_know_fails_loudly_instead_of_writing_nothing(
    tmp_path: Path,
) -> None:
    """Two ways a symbol yields no bars, and neither may pass as a finished dataset.

    A rejected request is not "this symbol had no sessions", and a provider that answers
    with an empty window for ten years running is not a tradable history either. Both stop
    the run; a zero-bar file would only be rejected by the harness later, after someone had
    already believed it.
    """
    server, opens = equity_and_index()
    start, end, now = span(opens)
    with pytest.raises(HistoryFetchError, match="chunk_fetch_failed:TCS.NS"):
        bulk_fetcher(server).fetch(TCS, start, end, now)

    server.add("TCS.NS", [])  # known to the provider, and empty in every window
    with pytest.raises(HistoryFetchError, match="no_closed_sessions:TCS.NS"):
        bulk_fetcher(server).fetch(TCS, start, end, now)
    assert not list(tmp_path.glob("*.json"))


def test_an_interrupted_run_resumes_from_the_cache_instead_of_refetching(
    tmp_path: Path,
) -> None:
    """Ten years across five symbols is sixty-odd requests; a rerun must not repeat them.

    The windows that arrived are on disk, so the second attempt asks only for the ones the
    first never got. Without this a rate limit turns every retry into the same crawl again,
    which is how a source starts refusing the operator altogether.
    """
    cache = tmp_path / "cache"
    server, opens = equity_and_index()
    start, end, now = span(opens)
    per_symbol = chunks_per_symbol(opens)
    assert per_symbol >= 2  # the range really is chunked, so resuming means something
    server.fail_windows = {per_symbol + 1}  # the symbol's second window

    with pytest.raises(HistoryFetchError):
        list(build_datasets(bulk_fetcher(server, cache_dir=cache), (INFY,), start=start,
                            end=end, years=2, fetched_at=now))
    first_attempt = len(server.requests)

    server.fail_windows = set()
    payload = one_dataset(server, opens, cache_dir=cache)
    resumed = len(server.requests) - first_attempt
    # The benchmark and the symbol's first window came off disk; the rest were refetched.
    assert resumed == per_symbol - 1
    from_cache = [chunk["from_cache"] for chunk in payload["provenance"]["chunks"]]
    assert from_cache == [True] + [False] * (per_symbol - 1)
    assert len(payload["bars"]) == len(opens)


def test_a_cache_entry_is_never_served_for_a_window_it_was_not_fetched_for(
    tmp_path: Path,
) -> None:
    """Otherwise a resume quietly re-labels one symbol's prices as another's.

    The entry carries the symbol and window it was fetched for and is checked against the
    request being served; a mismatch is a cache miss, never a substitution.
    """
    cache = tmp_path / "cache"
    server, opens = equity_and_index()
    one_dataset(server, opens, cache_dir=cache)
    entries = sorted(cache.glob("*.json"))
    assert entries

    hijacked = json.loads(entries[0].read_text())
    hijacked["provider_symbol"] = "SOMETHINGELSE.NS"
    entries[0].write_text(json.dumps(hijacked))

    before = len(server.requests)
    payload = one_dataset(server, opens, cache_dir=cache)
    assert len(server.requests) == before + 1  # refetched, not trusted
    assert len(payload["bars"]) == len(opens)


# ----------------------------------------------------- closed sessions and shape


def test_the_still_forming_session_is_not_written_as_a_closed_bar() -> None:
    """Yahoo serves today's partial bar beside the history, and it is not a daily bar yet.

    Writing it makes the final row of a dataset a high, low and close that still had hours
    left to move, and a backtest reading the last bar of its own file cannot tell.
    """
    opens = session_opens()
    server, _ = equity_and_index()
    # 12:00 IST on the last session: it has opened, and it closes at 15:30.
    mid_session = opens[-1] + timedelta(hours=2, minutes=45)
    history = bulk_fetcher(server).fetch(
        INFY, opens[0] - timedelta(days=1), opens[-1] + timedelta(days=1), mid_session
    )
    assert history.bars[-1].timestamp == opens[-2]
    assert len(history.bars) == len(opens) - 1


def test_the_written_file_loads_through_load_replay_dataset_and_the_harness_accepts_it(
    tmp_path: Path,
) -> None:
    """The output contract, proved against the loader rather than described in a docstring.

    ``load_replay_dataset`` and ``HistoricalReplayHarness._validate`` are what a real run
    puts this file through: timezone-aware stamps, strictly increasing, one instrument, and
    prices that satisfy ``Candle``'s own invariants.
    """
    server, opens = equity_and_index()
    path = write_dataset(tmp_path / "INFY.json", one_dataset(server, opens))

    dataset = load_replay_dataset(path, INFY)
    assert len(dataset.bars) == len(opens)
    assert len(dataset.benchmark_closes) == len(dataset.bars)
    assert all(bar.instrument == INFY for bar in dataset.bars)
    assert all(bar.timestamp.utcoffset() is not None for bar in dataset.bars)
    HistoricalReplayHarness._validate(dataset)
    assert len(HistoricalReplayHarness._benchmark_returns(dataset)) == len(dataset.bars) - 1
    # The provenance rides along in the same file and the loader passes over it.
    assert json.loads(path.read_text())["provenance"]["source"] == "yahoo-finance-chart-v8"


def test_the_provenance_counts_the_bars_that_were_actually_written() -> None:
    """A bar count taken from the request rather than the result is a lie about a gap."""
    opens = session_opens()
    server, _ = equity_and_index(blank=frozenset({opens[10]}))
    provenance = one_dataset(server, opens)["provenance"]

    assert provenance["bar_count"] == len(opens) - 1
    assert provenance["first_bar"] == opens[0].isoformat()
    assert provenance["last_bar"] == opens[-1].isoformat()
    assert provenance["requested_range"]["years"] == 2
    assert provenance["instrument"]["provider_symbol"] == "INFY.NS"
    assert provenance["benchmark"]["name"] == "NIFTY 50"
    assert provenance["benchmark"]["provider_symbol"] == "^NSEI"
    assert provenance["benchmark"]["bar_count"] == len(opens)
    assert sum(chunk["bars"] for chunk in provenance["chunks"]) == len(opens) - 1


# ------------------------------------------------------------ symbols and wiring


def test_the_default_symbol_set_is_read_from_the_founder_directives_not_restated_here(
    tmp_path: Path,
) -> None:
    """A copied watchlist keeps working while describing a portfolio nobody trades.

    The pilot's instruments live in deploy/founder-directives.example.json. This test
    compares against that file rather than against a list of its own, so the ingest and the
    pilot cannot drift apart without the file itself changing.
    """
    directives = json.loads(
        (ROOT / "deploy/founder-directives.example.json").read_text(encoding="utf-8")
    )
    expected = tuple(item["symbol"] for item in directives["watchlist"])
    assert expected  # the pilot has a watchlist at all
    assert tuple(item.symbol for item in watchlist_instruments()) == expected

    elsewhere = tmp_path / "directives.json"
    elsewhere.write_text(json.dumps({
        "allowed_markets": ["INDIA"],
        "allowed_asset_classes": ["EQUITY"],
        "watchlist": [{"symbol": "WIPRO", "market": "INDIA", "asset_class": "EQUITY",
                       "currency": "INR", "exchange": "NSE"}],
    }))
    assert tuple(item.symbol for item in watchlist_instruments(elsewhere)) == ("WIPRO",)

    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"watchlist": []}))
    with pytest.raises(HistoryFetchError, match="directives_have_no_watchlist"):
        watchlist_instruments(empty)


def test_the_nifty_index_ticker_is_not_given_an_nse_suffix() -> None:
    """``^NSEI.NS`` is not a symbol Yahoo knows, and the benchmark is the whole comparison.

    Suffixing it turns every dataset's ``benchmark_closes`` into an empty series, which is
    a baseline of nothing dressed up as a baseline.
    """
    assert yahoo_symbol("^NSEI", Market.INDIA) == "^NSEI"
    assert yahoo_symbol("INFY", Market.INDIA) == "INFY.NS"  # ordinary listings still suffix
    assert yahoo_symbol("INFY.NS", Market.INDIA) == "INFY.NS"
    assert yahoo_symbol("AAPL", Market.USA) == "AAPL"

    server, opens = equity_and_index()
    start, end, now = span(opens)
    history = bulk_fetcher(server).fetch(NIFTY_50.instrument, start, end, now)
    assert {symbol for symbol, _, _ in server.requests} == {"^NSEI"}
    assert len(history.bars) == len(opens)


def test_the_bulk_crawl_identifies_itself_and_runs_on_a_circuit_of_its_own() -> None:
    """A rate limit hit by a ten-year crawl must not halt the engine's own data feed.

    They are separate ``ResilientHttpClient`` instances, so the circuit the crawl opens is
    the crawl's. The User-Agent is the other half of being a good guest: an anonymous
    multi-year crawl is what gets a source throttled for everyone using it.
    """
    seen: list[dict[str, str] | None] = []

    class Inner:
        def request(self, method, url, *, params, headers, timeout_seconds, max_bytes):
            seen.append(headers)
            return HttpResponse(200, b"{}", {})

    def call(headers):
        UserAgentTransport(Inner()).request(
            "GET", "https://example.test", params=None, headers=headers,
            timeout_seconds=1.0, max_bytes=10,
        )

    call(None)
    assert seen[-1]["User-Agent"] == USER_AGENT
    call({"User-Agent": "operator"})
    assert seen[-1]["User-Agent"] == "operator"  # a caller's own header still wins

    first, second = default_fetcher(transport=Inner()), default_fetcher(transport=Inner())
    assert first.client is not second.client
    assert first.client.circuit_breaker is not second.client.circuit_breaker


def test_chunk_ranges_cover_the_span_once_with_no_gap_between_windows() -> None:
    """A chunker that skips a day silently drops every session inside it."""
    start = datetime(2015, 1, 1, tzinfo=timezone.utc)
    end = datetime(2025, 1, 1, tzinfo=timezone.utc)
    windows = chunk_ranges(start, end, 365)
    assert windows[0][0] == start
    assert windows[-1][1] == end
    assert all(left < right for left, right in windows)
    for (_, previous_end), (next_start, _) in zip(windows, windows[1:]):
        assert previous_end == next_start
    with pytest.raises(ValueError, match="end must be after start"):
        chunk_ranges(end, start, 365)
    with pytest.raises(ValueError, match="chunk days must be positive"):
        chunk_ranges(start, end, 0)


def test_a_symbol_with_one_paired_session_is_refused_rather_than_written() -> None:
    """One bar is not a series, and a one-bar file would only be rejected further on."""
    opens = session_opens(3)
    server = ChartServer()
    server.add("INFY.NS", opens, base=1000.0)
    server.add("^NSEI", opens, base=18000.0, skip=frozenset(opens[1:]))
    start, end, now = span(opens)
    bulk = bulk_fetcher(server)
    with pytest.raises(HistoryFetchError, match="insufficient_paired_sessions:INFY"):
        build_payload(
            bulk.fetch(INFY, start, end, now),
            bulk.fetch(NIFTY_50.instrument, start, end, now),
            requested_start=start,
            requested_end=end,
            years=1,
            fetched_at=now,
        )


# ----------------------------------------------------------------------- the CLI


def test_the_script_writes_one_loadable_file_per_symbol(tmp_path: Path) -> None:
    """End to end through the real argument parsing, with only the transport swapped.

    One file per symbol, because the harness validates one instrument per dataset, and
    every file has to come back out through ``load_replay_dataset``.
    """
    server, opens = cli_server()
    out = tmp_path / "datasets"
    assert run_cli(server, opens, out, chunk_days=400) == 0

    for instrument in (INFY, TCS):
        dataset = load_replay_dataset(out / f"{instrument.symbol}.json", instrument)
        HistoricalReplayHarness._validate(dataset)
        assert len(dataset.benchmark_closes) == len(dataset.bars) > 0


def test_the_script_exits_nonzero_and_writes_nothing_for_a_symbol_it_could_not_finish(
    tmp_path: Path,
) -> None:
    """The finished symbols stand; the one with a failed window gets no file at all.

    A run that reported success here would hand the operator a dataset with a hole in it
    and no way to know, which is worse than handing them no dataset.
    """
    server, opens = cli_server()
    # One window per symbol at this chunk size: benchmark, INFY, then TCS.
    server.fail_windows = {2}
    out = tmp_path / "datasets"
    assert run_cli(server, opens, out, chunk_days=800) == 2
    assert (out / "INFY.json").exists()
    assert not (out / "TCS.json").exists()


# ------------------------------------------- the dataset says what it holds, and is believed


def score_baselines(argv: list[str], tmp_path: Path, monkeypatch) -> dict:
    """Run the operator's ``baselines`` command and return the document it wrote."""
    from quant_ai.cli import main as cli_main

    monkeypatch.setenv("QUANT_AI_PAPER_DB", str(tmp_path / "ledger.db"))
    monkeypatch.setenv("PRAMANA_XAI_DIR", str(tmp_path / "xai"))
    assert cli_main(argv) == 0
    return json.loads((tmp_path / "xai" / "latest-baselines.json").read_text())


def test_a_fetched_dataset_is_scored_as_the_symbol_it_holds_not_a_hardcoded_one(
    tmp_path: Path, monkeypatch
) -> None:
    """The writer and the reader agree about what is in the file, end to end.

    ``load_replay_dataset`` stamps every bar with the instrument its caller passes in, and
    the CLI used to build that instrument from ``--market`` alone - RELIANCE for india,
    AAPL for anything else. So a run over ``INFY.json`` produced a table, a trial-register
    study (``baselines:RELIANCE:INDIA``) and a proof that all named RELIANCE while every
    price in them was Infosys's. Nothing in the output said so, and a second run over
    ``TCS.json`` collided with the first under the same study name.

    Nothing here is hand-shaped: the file is written by the real script and read by the
    real command, so a provenance block either side stops emitting or stops reading is a
    failure rather than a silently narrower test.
    """
    server, opens = cli_server()
    out = tmp_path / "datasets"
    assert run_cli(server, opens, out, chunk_days=CHUNK_DAYS) == 0

    for instrument in (INFY, TCS):
        payload = score_baselines(
            ["baselines", "--data", str(out / f"{instrument.symbol}.json")], tmp_path, monkeypatch
        )
        assert payload["instrument"] == {
            "symbol": instrument.symbol,
            "market": instrument.market.value,
        }
        assert payload["bars"] > 0


def test_a_market_flag_that_contradicts_the_dataset_is_refused_rather_than_obeyed(
    tmp_path: Path, monkeypatch
) -> None:
    """Two answers and no way to tell which is right is not a case for picking one.

    An operator who passes ``--market us`` at an NSE file has mixed up two things, and
    either resolution - trusting the flag and mispricing the friction, or trusting the file
    and ignoring what was typed - hides the mistake instead of reporting it.
    """
    from quant_ai.cli import main as cli_main

    server, opens = cli_server()
    out = tmp_path / "datasets"
    assert run_cli(server, opens, out, chunk_days=CHUNK_DAYS) == 0

    monkeypatch.setenv("QUANT_AI_PAPER_DB", str(tmp_path / "ledger.db"))
    monkeypatch.setenv("PRAMANA_XAI_DIR", str(tmp_path / "xai"))
    with pytest.raises(SystemExit) as refused:
        cli_main(["baselines", "--data", str(out / "INFY.json"), "--market", "us"])
    assert "contradicts" in str(refused.value) and "INFY" in str(refused.value)

    # The flag that agrees with the file is not an error, and does not change the answer.
    payload = score_baselines(
        ["baselines", "--data", str(out / "INFY.json"), "--market", "india"], tmp_path, monkeypatch
    )
    assert payload["instrument"]["symbol"] == "INFY"


def test_a_dataset_that_declares_nothing_is_refused_without_a_flag_not_scored_as_american(
    tmp_path: Path, monkeypatch
) -> None:
    """The silent US default, which is the half of the bug that had no symptom at all.

    ``--market`` carries no argparse default, but the resolver used to read an absent flag
    as ``us``. An NSE series scored that way pays the US fee schedule - no STT, no stamp
    duty, no depository charge - and annualises against the US session length. Both are
    wrong, both are invisible, and the resulting Sharpe is the one an operator would act on.

    A hand-written fixture that declares no provenance is still legitimate, so the fix is
    to require the flag rather than to reject the file.
    """
    from quant_ai.cli import main as cli_main

    bars = []
    moment = datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)
    for index in range(60):
        price = 100 + (index % 9)
        bars.append({
            "timestamp": (moment + timedelta(days=index)).isoformat(),
            "open": str(price), "high": str(price + 2), "low": str(price - 2),
            "close": str(price + 1), "volume": "100000",
        })
    undeclared = tmp_path / "undeclared.json"
    undeclared.write_text(json.dumps({"bars": bars}))

    monkeypatch.setenv("QUANT_AI_PAPER_DB", str(tmp_path / "ledger.db"))
    monkeypatch.setenv("PRAMANA_XAI_DIR", str(tmp_path / "xai"))
    with pytest.raises(SystemExit) as refused:
        cli_main(["baselines", "--data", str(undeclared)])
    assert "--market is required" in str(refused.value)

    payload = score_baselines(
        ["baselines", "--data", str(undeclared), "--market", "india"], tmp_path, monkeypatch
    )
    assert payload["instrument"]["market"] == "INDIA"


def test_a_declaration_that_is_absent_abstains_and_one_that_is_broken_raises(
    tmp_path: Path,
) -> None:
    """Abstain on silence, refuse on a lie. The two are not the same failure.

    A file with no provenance predates the block and cannot be blamed for it; the caller
    is told nothing and says what it knows. A file that carries the block and fills it with
    something that is not a market has made a claim that cannot be honoured, and handing
    back ``None`` for it would quietly route it into the same fallback as the honest silent
    file - which is how a wrong declaration becomes a US default.
    """
    silent = tmp_path / "silent.json"
    silent.write_text(json.dumps({"bars": []}))
    assert dataset_instrument(silent) is None

    no_block = tmp_path / "no-instrument.json"
    no_block.write_text(json.dumps({"bars": [], "provenance": {"source": "somewhere"}}))
    assert dataset_instrument(no_block) is None

    assert dataset_instrument(tmp_path / "bars.csv") is None

    not_a_mapping = tmp_path / "not-a-mapping.json"
    not_a_mapping.write_text(json.dumps({"provenance": {"instrument": "INFY"}}))
    with pytest.raises(TypeError, match="dataset_instrument_malformed"):
        dataset_instrument(not_a_mapping)

    for broken in (
        {"symbol": "INFY", "market": "MOON", "asset_class": "EQUITY",
         "exchange": "NSE", "currency": "INR"},
        {"symbol": "INFY", "market": "INDIA", "asset_class": "DREAMS",
         "exchange": "NSE", "currency": "INR"},
        {"symbol": "INFY", "market": "INDIA", "exchange": "NSE", "currency": "INR"},
    ):
        lying = tmp_path / "lying.json"
        lying.write_text(json.dumps({"provenance": {"instrument": broken}}))
        with pytest.raises(ValueError, match="dataset_instrument_malformed"):
            dataset_instrument(lying)
