"""The fetcher's output must be the study's input, proved rather than assumed.

scripts/fetch_historical_bars.py writes one dataset per symbol with build_payload and
write_dataset. run_universe_study globs that directory and reads each file back. Those are
two modules written at different times by different hands, and nothing until now ran a file
from one through the other. If the shapes have drifted, the person who finds out should not
be the operator with a fetched universe and a deadline.

No network: the fetcher's HTTP layer is replaced by a stub, and everything downstream of it
is the real code path.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from random import Random

from quant_ai.backtesting.history import (
    NIFTY_50,
    SymbolHistory,
    build_payload,
    write_dataset,
)
from quant_ai.domain.models import AssetClass, Instrument, Market
from quant_ai.marketdata.models import Candle
from quant_ai.marketdata.universe_manifest import SCHEMA
from quant_ai.research.study_runner import run_universe_study

START = datetime(2016, 1, 4, tzinfo=timezone.utc)
SESSIONS = 1500


def bars(instrument: Instrument, *, seed: int, pull: float) -> tuple[Candle, ...]:
    rng = Random(seed)
    closes = [100.0]
    out: list[Candle] = []
    day = START
    while len(out) < SESSIONS:
        day += timedelta(days=1)
        if day.weekday() >= 5:          # the fetcher only ever sees trading sessions
            continue
        window = closes[-20:]
        average = sum(window) / len(window)
        gap = (closes[-1] - average) / average
        closes.append(max(closes[-1] * (1 + rng.gauss(0, 0.011) - pull * gap), 1.0))
        close = Decimal(str(round(closes[-1], 4)))
        span = close * Decimal("0.008")
        out.append(Candle(
            instrument, day.replace(hour=10),
            open=close, high=close + span, low=close - span, close=close,
            volume=Decimal(rng.randint(500_000, 2_000_000)),
        ))
    return tuple(out)


def fetched_dataset(directory, symbol: str, *, seed: int, pull: float) -> None:
    """Exactly what scripts/fetch_historical_bars.py writes, minus the HTTP call."""
    instrument = Instrument(symbol, Market.INDIA, AssetClass.EQUITY, "INR", "NSE")
    symbol_bars = bars(instrument, seed=seed, pull=pull)
    index_bars = bars(NIFTY_50.instrument, seed=7, pull=0.0)
    payload = build_payload(
        SymbolHistory(instrument, symbol_bars, ()),
        SymbolHistory(NIFTY_50.instrument, index_bars, ()),
        benchmark=NIFTY_50,
        requested_start=START,
        requested_end=symbol_bars[-1].timestamp,
        years=6,
        fetched_at=symbol_bars[-1].timestamp,
    )
    write_dataset(directory / f"{symbol}.json", payload)


def test_a_fetched_dataset_is_readable_by_the_universe_study(tmp_path) -> None:
    """The integration nobody had run: fetcher output straight into the research loop."""
    datasets = tmp_path / "datasets"
    datasets.mkdir()
    for index, (symbol, pull) in enumerate(
        [("AAA", 0.20), ("BBB", 0.18), ("CCC", 0.0), ("FAILED", 0.0)]
    ):
        fetched_dataset(datasets, symbol, seed=10 + index, pull=pull)

    listings = [
        {"symbol": s, "market": "INDIA", "listed_on": "2016-01-04"}
        for s in ("AAA", "BBB", "CCC")
    ]
    listings.append({
        "symbol": "FAILED", "market": "INDIA", "listed_on": "2016-01-04",
        "delisted_on": "2018-06-01", "delisting_reason": "insolvency",
    })
    book = tmp_path / "manifest.json"
    book.write_text(json.dumps({
        "schema": SCHEMA, "source": "NSE listing history, exchange archive 2016-2022",
        "coverage_from": "2016-01-04", "coverage_to": "2022-12-31", "listings": listings,
    }))

    report = run_universe_study(datasets, book, register=tmp_path / "register.jsonl")

    # The instrument came out of the provenance block the fetcher wrote, not a guess.
    assert {item["symbol"] for item in report["results"]} == {"AAA", "BBB", "CCC", "FAILED"}
    assert report["instruments_skipped"] == []
    # The delisted name stopped where the manifest says it stopped.
    failed = next(item for item in report["results"] if item["symbol"] == "FAILED")
    assert failed["bars_dropped_after_delisting"] > 0
    # And every name was charged for the whole search.
    assert report["trial_register"]["candidate_trials"] == 24 * 4


def test_the_fetchers_provenance_block_carries_what_the_study_needs(tmp_path) -> None:
    """A narrower assertion on the contract itself, so a drift says which field moved."""
    datasets = tmp_path / "datasets"
    datasets.mkdir()
    fetched_dataset(datasets, "AAA", seed=1, pull=0.0)
    payload = json.loads((datasets / "AAA.json").read_text())

    assert payload.get("bars"), "the study reads payload['bars']"
    declared = payload["provenance"]["instrument"]
    assert set(declared) >= {"symbol", "market", "asset_class", "currency", "exchange"}
    first = payload["bars"][0]
    assert set(first) >= {"timestamp", "open", "high", "low", "close", "volume"}
    # Prices are strings on purpose: a float round trip would quietly re-price a bar.
    assert all(isinstance(first[key], str) for key in ("open", "high", "low", "close"))
