"""The study runner has to discount its own search, including across runs.

experiment.py listed this as an open limitation in its own report: "no reported statistic
here is corrected for that multiplicity". The register was already counting the candidates.
Nothing read the count back until selection_corrected.
"""

from __future__ import annotations

import math
from random import Random

import pytest

from quant_ai.validation.experiment import evaluate, selection_corrected


def random_walk(count: int = 400, seed: int = 4) -> list[float]:
    """No edge by construction. Anything a study finds here is selection noise."""
    rng = Random(seed)
    price = 1000.0
    prices = []
    for _ in range(count):
        price *= 1 + rng.gauss(0, 0.012)
        prices.append(price)
    return prices


def test_the_report_retains_what_the_correction_needs() -> None:
    report = evaluate(random_walk())
    assert len(report["holdout_returns"]) == report["holdout"]["observations"]
    # Every candidate the search evaluated, not only the one it kept.
    assert set(report["candidate_sharpes"]) == {"5", "10", "20", "50"}


def test_a_profitable_looking_random_walk_does_not_clear_the_gate() -> None:
    report = evaluate(random_walk())
    corrected = selection_corrected(report, int(report["candidate_trials"]))

    assert corrected["corrected"] is True
    assert corrected["clears_statistical_gate"] is False
    assert corrected["deflated_sharpe"] < 0.95
    # The bar a search this size sets is at or above what the study actually achieved.
    assert corrected["selection_benchmark_sharpe"] >= corrected["observed_sharpe"] * 0.9


def test_reusing_the_holdout_raises_the_bar_and_lowers_the_verdict() -> None:
    """The register's cumulative count is the point: the tenth run has looked ten times."""
    report = evaluate(random_walk())
    first = selection_corrected(report, 64)
    later = selection_corrected(report, 320)

    assert later["selection_benchmark_sharpe"] > first["selection_benchmark_sharpe"]
    assert later["deflated_sharpe"] < first["deflated_sharpe"]
    # Same data, same result, honestly discounted for how often it has been looked at.
    assert first["observed_sharpe"] == later["observed_sharpe"]


def test_the_correction_uses_the_cumulative_count_not_this_run() -> None:
    report = evaluate(random_walk())
    corrected = selection_corrected(report, 500)
    assert corrected["cumulative_candidate_trials"] == 500
    assert corrected["cumulative_candidate_trials"] != report["candidate_trials"]


def test_dropping_the_losing_candidates_would_lower_the_bar() -> None:
    """Which is why candidate_sharpes keeps them. This asserts the incentive, not a behaviour."""
    report = evaluate(random_walk())
    everything = selection_corrected(report, 64)

    survivors_only = dict(report)
    best = max(report["candidate_sharpes"].values(), key=lambda v: (v is not None, v))
    survivors_only["candidate_sharpes"] = {
        name: value for name, value in report["candidate_sharpes"].items()
        if value is not None and value > best * 0.9
    }
    if len(survivors_only["candidate_sharpes"]) >= 2:
        trimmed = selection_corrected(survivors_only, 64)
        assert trimmed["selection_benchmark_sharpe"] < everything["selection_benchmark_sharpe"]


def test_a_study_too_small_to_measure_says_so_rather_than_guessing() -> None:
    assert selection_corrected({}, 64) == {
        "corrected": False,
        "reason": "too few holdout observations or evaluated candidates to measure selection bias",
    }
    assert selection_corrected(
        {"holdout_returns": [0.01], "candidate_sharpes": {"5": 0.1, "10": 0.2}}, 64
    )["corrected"] is False


def test_a_flat_holdout_is_refused_rather_than_scored() -> None:
    flat = {"holdout_returns": [0.0] * 40, "candidate_sharpes": {"5": 0.1, "10": 0.2}}
    result = selection_corrected(flat, 64)
    assert result["corrected"] is False
    assert "variance" in result["reason"]


def test_an_unreachable_track_record_is_reported_as_infinite() -> None:
    report = evaluate(random_walk())
    corrected = selection_corrected(report, 5000)
    assert math.isinf(corrected["minimum_track_record"])
    assert corrected["clears_statistical_gate"] is False


def test_the_correction_publishes_what_it_does_not_cover() -> None:
    report = evaluate(random_walk())
    limitation = selection_corrected(report, 64)["limitation"]
    assert "outside the register" in limitation
    assert "survivorship" in limitation


def test_candidates_with_no_dispersion_are_dropped_not_scored_as_zero() -> None:
    mixed = {
        "holdout_returns": [0.01, -0.02, 0.015, -0.005] * 10,
        "candidate_sharpes": {"5": 0.1, "10": None, "20": 0.25},
    }
    result = selection_corrected(mixed, 64)
    assert result["corrected"] is True


def test_evaluate_still_refuses_an_invalid_configuration() -> None:
    with pytest.raises(ValueError, match="Insufficient data or invalid"):
        evaluate(random_walk(50))


def test_the_track_record_length_is_the_same_condition_as_the_deflated_sharpe() -> None:
    """Documented as a test because it is easy to add back as a second check.

    PSR >= 0.95 at a benchmark and n >= MinTRL at that benchmark and 95% confidence are the
    same inequality. Reporting both is useful; gating on both implies two checks where there
    is one, and invites a later edit that changes one and not the other.
    """
    report = evaluate(random_walk())
    for trials in (8, 64, 320, 5000):
        corrected = selection_corrected(report, trials)
        by_probability = corrected["deflated_sharpe"] >= 0.95
        by_length = corrected["observations"] >= corrected["minimum_track_record"]
        assert by_probability == by_length, (
            f"the two conditions disagreed at {trials} trials: "
            f"deflated={corrected['deflated_sharpe']}, "
            f"n={corrected['observations']}, needed={corrected['minimum_track_record']}"
        )
        assert corrected["clears_statistical_gate"] == by_probability
