"""A universe that remembers its failures, and a search charged for every name in it."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from random import Random

import pytest

from quant_ai.marketdata.universe_manifest import (
    SCHEMA,
    load_universe_manifest,
    parse_universe_manifest,
)
from quant_ai.research.study_runner import run_universe_study

BASE = datetime(2016, 1, 1, tzinfo=timezone.utc)
SOURCE = "NSE listing history, exchange archive 2016-2020"


def write_dataset(directory: Path, symbol: str, *, seed: int, pull: float, count: int = 1500) -> None:
    rng = Random(seed)
    closes = [100.0]
    bars = []
    for index in range(count):
        window = closes[-20:]
        average = sum(window) / len(window)
        gap = (closes[-1] - average) / average
        closes.append(max(closes[-1] * (1 + rng.gauss(0, 0.011) - pull * gap), 1.0))
        close = round(closes[-1], 4)
        span = round(close * 0.008, 4)
        bars.append({
            "timestamp": (BASE + timedelta(days=index)).isoformat(),
            "open": str(close), "high": str(round(close + span, 4)),
            "low": str(round(close - span, 4)), "close": str(close),
            "volume": str(rng.randint(800, 4000)),
        })
    (directory / f"{symbol}.json").write_text(json.dumps({
        "bars": bars,
        "provenance": {"instrument": {
            "symbol": symbol, "market": "INDIA", "asset_class": "EQUITY",
            "currency": "INR", "exchange": "NSE",
        }},
    }))


def manifest(path: Path, symbols: list[str], *, delisted: dict[str, str] | None = None) -> Path:
    listings = []
    for symbol in symbols:
        entry = {"symbol": symbol, "market": "INDIA", "listed_on": "2016-01-01"}
        if delisted and symbol in delisted:
            entry["delisted_on"] = delisted[symbol]
            entry["delisting_reason"] = "insolvency"
        listings.append(entry)
    path.write_text(json.dumps({
        "schema": SCHEMA, "source": SOURCE,
        "coverage_from": "2016-01-01", "coverage_to": "2020-06-01",
        "listings": listings,
    }))
    return path


def universe_fixture(tmp_path: Path, *, with_delisting: bool = True) -> tuple[Path, Path]:
    datasets = tmp_path / "datasets"
    datasets.mkdir()
    for index, (symbol, pull) in enumerate(
        [("AAA", 0.20), ("BBB", 0.18), ("CCC", 0.0), ("DDD", 0.0), ("FAILED", 0.0)]
    ):
        write_dataset(datasets, symbol, seed=10 + index, pull=pull)
    names = ["AAA", "BBB", "CCC", "DDD", "FAILED"]
    gone = {"FAILED": "2018-06-01"} if with_delisting else None
    return datasets, manifest(tmp_path / "manifest.json", names, delisted=gone)


# --------------------------------------------------------- the universe actually scopes


def test_bars_after_a_delisting_are_dropped_and_counted(tmp_path) -> None:
    """A study that trades a delisted name's later bars is the survivorship it exists to stop."""
    datasets, book = universe_fixture(tmp_path)
    report = run_universe_study(datasets, book, register=tmp_path / "register.jsonl")

    failed = next(item for item in report["results"] if item["symbol"] == "FAILED")
    survivor = next(item for item in report["results"] if item["symbol"] == "AAA")
    assert failed["bars_dropped_after_delisting"] > 500
    assert failed["bars"] < survivor["bars"]
    assert survivor["bars_dropped_after_delisting"] == 0


def test_a_dataset_outside_the_manifest_is_skipped_not_studied(tmp_path) -> None:
    """Otherwise survivorship arrives by the back door: a file on disk nobody declared."""
    datasets, _ = universe_fixture(tmp_path)
    write_dataset(datasets, "UNDECLARED", seed=99, pull=0.30)
    book = manifest(tmp_path / "partial.json", ["AAA", "BBB", "CCC", "DDD", "FAILED"],
                    delisted={"FAILED": "2018-06-01"})

    report = run_universe_study(datasets, book, register=tmp_path / "register.jsonl")
    studied = {item["symbol"] for item in report["results"]}
    assert "UNDECLARED" not in studied
    assert any("not in the universe manifest" in item["reason"]
               for item in report["instruments_skipped"])


def test_a_universe_that_records_failures_can_finally_pass(tmp_path) -> None:
    """The counter-test for the whole survivorship apparatus: a green light must be reachable."""
    datasets, book = universe_fixture(tmp_path, with_delisting=True)
    report = run_universe_study(datasets, book, register=tmp_path / "register.jsonl")

    assert report["universe"]["verdict"] == "plausible"
    assert report["clears_every_gate"] is True, report["blockers"]
    assert report["best"] in {"AAA", "BBB"}


def test_the_same_data_without_delisting_records_is_blocked(tmp_path) -> None:
    """Identical bars, identical statistics. Only the universe's honesty changes."""
    datasets, _ = universe_fixture(tmp_path)
    honest = manifest(tmp_path / "honest.json", ["AAA", "BBB", "CCC", "DDD", "FAILED"],
                      delisted={"FAILED": "2018-06-01"})
    flattering = manifest(tmp_path / "flattering.json",
                          ["AAA", "BBB", "CCC", "DDD", "FAILED"], delisted=None)

    good = run_universe_study(datasets, honest, register=tmp_path / "a.jsonl")
    bad = run_universe_study(datasets, flattering, register=tmp_path / "b.jsonl")

    assert good["clears_every_gate"] is True
    assert bad["universe"]["verdict"] == "survivor_only"
    assert bad["clears_every_gate"] is False


# ------------------------------------------------------- the search pays for its width


def test_studying_more_names_charges_for_more_names(tmp_path) -> None:
    """Reporting the best of fifty is fifty chances to find something, and the bar knows."""
    datasets, book = universe_fixture(tmp_path)
    report = run_universe_study(datasets, book, register=tmp_path / "register.jsonl")
    assert report["trial_register"]["candidate_trials"] == 24 * 5
    for item in report["results"]:
        assert item["study"]["candidate_trials"] == 120


def test_rerunning_the_universe_study_charges_again(tmp_path) -> None:
    datasets, book = universe_fixture(tmp_path)
    register = tmp_path / "register.jsonl"
    first = run_universe_study(datasets, book, register=register)
    second = run_universe_study(datasets, book, register=register)

    assert second["trial_register"]["candidate_trials"] == 240
    best_first = next(r for r in first["results"] if r["symbol"] == first["best"])
    best_second = next(r for r in second["results"] if r["symbol"] == first["best"])
    assert best_second["study"]["deflated_sharpe"] <= best_first["study"]["deflated_sharpe"]


# ------------------------------------------------------------------ manifest validation


def test_a_manifest_must_say_where_its_listing_history_came_from() -> None:
    with pytest.raises(ValueError, match="name where its listing history came from"):
        parse_universe_manifest({
            "schema": SCHEMA, "source": "", "coverage_from": "2016-01-01",
            "coverage_to": "2020-01-01",
            "listings": [{"symbol": "A", "market": "INDIA", "listed_on": "2016-01-01"}],
        })


def test_a_manifest_refuses_a_duplicate_listing() -> None:
    with pytest.raises(ValueError, match="listed twice"):
        parse_universe_manifest({
            "schema": SCHEMA, "source": SOURCE, "coverage_from": "2016-01-01",
            "coverage_to": "2020-01-01", "listings": [
                {"symbol": "A", "market": "INDIA", "listed_on": "2016-01-01"},
                {"symbol": "A", "market": "INDIA", "listed_on": "2017-01-01"},
            ],
        })


def test_a_manifest_refuses_a_malformed_date_rather_than_guessing() -> None:
    with pytest.raises(ValueError, match="listed_on is not an ISO date"):
        parse_universe_manifest({
            "schema": SCHEMA, "source": SOURCE, "coverage_from": "2016-01-01",
            "coverage_to": "2020-01-01",
            "listings": [{"symbol": "A", "market": "INDIA", "listed_on": "last tuesday"}],
        })


def test_a_manifest_refuses_the_wrong_schema_and_the_wrong_type() -> None:
    with pytest.raises(ValueError, match="schema must be"):
        parse_universe_manifest({"schema": "something.else", "source": SOURCE})
    with pytest.raises(TypeError, match="must be a JSON object"):
        parse_universe_manifest([])


def test_a_manifest_round_trips_through_a_file(tmp_path) -> None:
    path = manifest(tmp_path / "m.json", ["AAA", "BBB"], delisted={"BBB": "2018-06-01"})
    universe = load_universe_manifest(path)
    assert universe.source == SOURCE
    assert {item.symbol for item in universe.listings} == {"AAA", "BBB"}
    assert universe.audit().delisted == 1


def test_an_empty_dataset_directory_is_refused(tmp_path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    book = manifest(tmp_path / "m.json", ["AAA"])
    with pytest.raises(ValueError, match="no datasets found"):
        run_universe_study(empty, book, register=tmp_path / "register.jsonl")
