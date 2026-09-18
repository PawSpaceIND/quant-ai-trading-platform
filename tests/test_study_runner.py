"""Running the study on real bars must not let the result outrun the data it came from."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from random import Random

import pytest

from quant_ai.research.study_runner import run

START = datetime(2020, 1, 1, tzinfo=timezone.utc)
PROVENANCE = {"instrument": {
    "symbol": "INFY", "market": "INDIA", "asset_class": "EQUITY",
    "currency": "INR", "exchange": "NSE",
}}


def dataset(path: Path, *, count: int = 1400, pull: float = 0.20, seed: int = 3) -> Path:
    """Bars in the exact shape backtesting/history.py writes and replay.py reads back."""
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
            "timestamp": (START + timedelta(days=index)).isoformat(),
            "open": str(close), "high": str(round(close + span, 4)),
            "low": str(round(close - span, 4)), "close": str(close),
            "volume": str(rng.randint(800, 4000)),
        })
    path.write_text(json.dumps({"bars": bars, "provenance": PROVENANCE}))
    return path


def test_a_real_edge_still_fails_on_a_survivor_only_universe(tmp_path) -> None:
    """The point of the whole runner: good statistics on bad data are still bad."""
    report = run(dataset(tmp_path / "INFY.json"), register=tmp_path / "register.jsonl")

    assert report["study"]["clears_statistical_gate"] is True, report["study"]["reasons"]
    assert report["universe"]["verdict"] == "survivor_only"
    assert report["clears_every_gate"] is False
    assert any("not research-grade" in blocker for blocker in report["blockers"])


def test_the_universe_is_audited_rather_than_omitted(tmp_path) -> None:
    """A report that simply leaves the universe out reads as one whose universe was fine."""
    report = run(dataset(tmp_path / "INFY.json"), register=tmp_path / "register.jsonl")
    assert "universe" in report
    assert report["universe"]["listings"] == 1
    assert report["universe"]["delisted"] == 0


def test_running_again_charges_for_looking_again(tmp_path) -> None:
    """Trials are registered before the verdict, so the second run is charged for two searches."""
    source = dataset(tmp_path / "INFY.json")
    register = tmp_path / "register.jsonl"

    first = run(source, register=register)
    second = run(source, register=register)

    assert first["trial_register"]["candidate_trials"] == 24
    assert second["trial_register"]["candidate_trials"] == 48
    assert second["study"]["candidate_trials"] == 48
    # Same bars, same result, honestly discounted for having been looked at twice.
    assert second["study"]["out_of_sample_sharpe"] == first["study"]["out_of_sample_sharpe"]
    assert second["study"]["deflated_sharpe"] <= first["study"]["deflated_sharpe"]


def test_the_instrument_is_read_from_the_dataset_not_guessed(tmp_path) -> None:
    report = run(dataset(tmp_path / "INFY.json"), register=tmp_path / "register.jsonl")
    assert report["symbol"] == "INFY"
    assert report["market"] == "INDIA"


def test_a_dataset_that_does_not_say_what_it_holds_is_refused(tmp_path) -> None:
    """Guessing produces a report naming one security over another security's prices."""
    silent = tmp_path / "unnamed.json"
    silent.write_text(json.dumps({"bars": [{
        "timestamp": START.isoformat(), "open": "1", "high": "1",
        "low": "1", "close": "1", "volume": "1",
    }]}))
    with pytest.raises(ValueError, match="does not name the instrument"):
        run(silent, register=tmp_path / "register.jsonl")


def test_an_empty_dataset_is_refused(tmp_path) -> None:
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"bars": [], "provenance": PROVENANCE}))
    with pytest.raises(ValueError, match="a study needs a history"):
        run(empty, register=tmp_path / "register.jsonl")


def test_the_report_pins_the_bytes_it_was_computed_from(tmp_path) -> None:
    source = dataset(tmp_path / "INFY.json")
    report = run(source, register=tmp_path / "register.jsonl")
    import hashlib
    assert report["data_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert report["trial_register"]["distinct_datasets"] == 1


def test_no_arrangement_of_these_numbers_approves_anything(tmp_path) -> None:
    report = run(dataset(tmp_path / "INFY.json"), register=tmp_path / "register.jsonl")
    assert report["promotion_approved"] is False
    assert "never approves promotion or live trading" in report["limitation"]
