"""Whether the forecasts Atlas records were any good, and whether the answer can be trusted.

A scorer is worth exactly as much as its resistance to flattering the thing it scores.
These tests are mostly about that: a forecast that has only learned the base rate must
score zero skill, rows written under different mappings must never be pooled, each row
must be judged against the cost and horizon it was actually written with, and an
unresolved outcome must be excluded rather than counted as a miss.

The proper-scoring-rule property is pinned directly, because it is the reason these two
metrics were chosen over accuracy: stating your true belief has to beat both hedging
towards 0.5 and exaggerating away from it.
"""

from __future__ import annotations

import random
from decimal import Decimal

from quant_ai.agents.forecast import BASIS, HORIZON_SECONDS
from quant_ai.analytics import forecast_scoring as scoring


def row(probability: str | None, forward: str | None, *, basis: str = BASIS,
        horizon: int | None = HORIZON_SECONDS, cost: str = "10") -> dict:
    return {
        "forecast_probability_up": probability,
        "forecast_horizon_seconds": horizon,
        "forecast_basis": basis,
        "forecast_cost_bps": cost,
        "forward_return_60m": forward,
    }


def rows_for(pairs: list[tuple[str, str]], **kwargs) -> list[dict]:
    return [row(p, f, **kwargs) for p, f in pairs]


# ------------------------------------------------------------------ the outcome


def test_the_outcome_is_the_event_the_forecast_named_not_the_raw_sign() -> None:
    """Cost is part of the claim. A move that does not clear it did not happen."""
    # 10 bps is 0.001. A forward return of 0.0005 is positive and still a miss.
    assert scoring.outcome_of(row("0.60", "0.0005")) == (Decimal("0.60"), 0)
    assert scoring.outcome_of(row("0.60", "0.0011")) == (Decimal("0.60"), 1)
    # Exactly at the hurdle is not through it.
    assert scoring.outcome_of(row("0.60", "0.001")) == (Decimal("0.60"), 0)
    # The row's own stored cost governs, not the current policy constant.
    assert scoring.outcome_of(row("0.60", "0.0005", cost="1")) == (Decimal("0.60"), 1)


def test_a_row_is_never_scored_against_a_horizon_it_did_not_name() -> None:
    # A future basis forecasting a horizon with no resolver column is unscoreable, not
    # scored against the 60-minute column that happens to be filled.
    assert scoring.outcome_of(row("0.60", "0.02", horizon=86400)) is None
    assert scoring.outcome_of(row("0.60", "0.02", horizon=None)) is None
    # An unresolved outcome is excluded, not counted as a miss.
    assert scoring.outcome_of(row("0.60", None)) is None
    # A probability outside the mapping's own clamp is not a forecast this scorer trusts.
    assert scoring.outcome_of(row("0.00", "0.02")) is None
    assert scoring.outcome_of(row("1.00", "0.02")) is None


def test_unscoreable_rows_are_counted_by_reason_rather_than_quietly_dropped() -> None:
    result = scoring.summarize([
        row(None, "0.02"), row("0.6", None), row("0.6", "0.02", horizon=86400),
        row("0.00", "0.02"), row("0.6", "0.02"),
    ])
    assert result["decisions"] == 5
    assert result["with_forecast"] == 4
    assert result["unscoreable"] == {
        "no_forecast": 1, "unknown_horizon": 1, "outcome_unresolved": 1, "invalid_probability": 1,
    }


# ------------------------------------------------------------------ the metrics


def test_a_perfect_and_a_perfectly_wrong_forecaster_bracket_the_scale() -> None:
    perfect = scoring.score_basis(rows_for([("0.95", "0.05"), ("0.05", "-0.05")] * 20))
    assert perfect["brier_score"] < 0.01
    assert perfect["skill_vs_base_rate"] > 0.95

    backwards = scoring.score_basis(rows_for([("0.05", "0.05"), ("0.95", "-0.05")] * 20))
    assert backwards["brier_score"] > 0.8
    # Worse than knowing nothing, which must read as negative rather than as zero.
    assert backwards["skill_vs_base_rate"] < -2


def test_a_forecaster_that_only_knows_the_base_rate_scores_no_skill() -> None:
    """The point of the base-rate baseline. This forecaster beats a coin and knows nothing."""
    # 70% of these resolve true, and every forecast is a flat 0.70.
    pairs = [("0.70", "0.05")] * 70 + [("0.70", "-0.05")] * 30
    result = scoring.score_basis(rows_for(pairs))
    assert result["base_rate"] == 0.7
    assert abs(result["skill_vs_base_rate"]) < 1e-9, "a flat base-rate forecast has no skill"
    assert result["skill_vs_coin_flip"] > 0.15, "yet it comfortably beats a coin"
    # It is perfectly calibrated and separates nothing, which the decomposition must show.
    assert result["decomposition"]["reliability"] < 1e-9
    assert result["decomposition"]["resolution"] < 1e-9


def test_brier_and_log_score_are_proper_so_honesty_beats_hedging_and_exaggeration() -> None:
    """The reason these two metrics were chosen. A forecaster cannot game either one."""
    generator = random.Random(20260921)
    truth = Decimal("0.70")
    outcomes = [1 if generator.random() < 0.7 else 0 for _ in range(4000)]
    forward = {1: "0.05", 0: "-0.05"}

    def scored(stated: str) -> dict:
        return scoring.score_basis([row(stated, forward[o]) for o in outcomes])

    honest = scored(str(truth))
    for other in ("0.55", "0.60", "0.80", "0.90"):
        alternative = scored(other)
        assert honest["brier_score"] < alternative["brier_score"], f"{other} beat the truth on Brier"
        assert honest["log_score"] < alternative["log_score"], f"{other} beat the truth on log score"


def test_the_decomposition_reconstructs_the_brier_score_it_explains() -> None:
    pairs = [("0.9", "0.05")] * 40 + [("0.9", "-0.05")] * 10 + [("0.2", "0.05")] * 5 + [("0.2", "-0.05")] * 45
    result = scoring.score_basis(rows_for(pairs))
    d = result["decomposition"]
    rebuilt = d["reliability"] - d["resolution"] + d["uncertainty"] + d["within_bin"]
    # The published figures reconcile exactly, not to within a rounding tolerance.
    assert round(rebuilt, 6) == result["brier_score"]
    # These forecasts separate the two groups, so resolution must be substantial.
    assert d["resolution"] > 0.1
    # Every forecast in a bin is the same number here, so the classic three-term identity
    # already holds and there is nothing left over.
    assert abs(d["within_bin"]) < 1e-9


def test_the_decomposition_stays_exact_when_forecasts_vary_inside_a_bin() -> None:
    """The case the three-term identity does not cover, and the reason for within_bin.

    Murphy's identity is exact for a forecaster whose outputs are the bin labels. Real
    forecasts are continuous, so binning them loses the spread inside each bin and the
    three terms fall short of the Brier score they claim to explain. Reported as a fourth
    term the reconstruction is exact again, and the shortfall says how coarse the bins are
    for this forecaster rather than sitting there as a silent discrepancy.
    """
    generator = random.Random(20260922)
    pairs = []
    for _ in range(600):
        probability = round(generator.uniform(0.06, 0.94), 4)
        hit = generator.random() < probability
        pairs.append((str(probability), "0.05" if hit else "-0.05"))
    result = scoring.score_basis(rows_for(pairs))
    d = result["decomposition"]
    three_term = d["reliability"] - d["resolution"] + d["uncertainty"]
    assert abs(three_term - result["brier_score"]) > 1e-6, "this sample must exercise the gap"
    assert round(three_term + d["within_bin"], 6) == result["brier_score"]


def test_the_reliability_table_reports_what_each_bin_actually_did() -> None:
    pairs = [("0.85", "0.05")] * 8 + [("0.85", "-0.05")] * 2 + [("0.15", "-0.05")] * 10
    result = scoring.score_basis(rows_for(pairs))
    bins = {r["lower"]: r for r in result["reliability"]}
    assert bins[0.8]["forecasts"] == 10
    assert bins[0.8]["mean_forecast"] == 0.85
    assert bins[0.8]["observed_frequency"] == 0.8
    assert bins[0.1]["forecasts"] == 10
    assert bins[0.1]["observed_frequency"] == 0.0
    # An empty bin reports no frequency rather than a zero it never observed.
    assert bins[0.5]["forecasts"] == 0
    assert bins[0.5]["observed_frequency"] is None


# ------------------------------------------------------------------ the guardrails


def test_bases_are_scored_apart_so_a_refit_cannot_borrow_the_old_mapping_s_record() -> None:
    good = rows_for([("0.95", "0.05"), ("0.05", "-0.05")] * 20, basis="old.v1")
    bad = rows_for([("0.95", "-0.05"), ("0.05", "0.05")] * 20, basis="new.v2")
    result = scoring.summarize(good + bad)
    assert [b["basis"] for b in result["by_basis"]] == ["new.v2", "old.v1"]
    by_basis = {b["basis"]: b for b in result["by_basis"]}
    assert by_basis["old.v1"]["skill_vs_base_rate"] > 0.9
    assert by_basis["new.v2"]["skill_vs_base_rate"] < 0
    # Pooling would have produced one mediocre curve describing neither mapping.
    assert by_basis["old.v1"]["scored"] == by_basis["new.v2"]["scored"] == 40


def test_a_small_sample_prints_its_numbers_and_claims_no_skill_from_them() -> None:
    result = scoring.score_basis(rows_for([("0.95", "0.05")] * 5))
    assert result["scored"] == 5
    assert result["insufficient_sample"] is True
    assert result["brier_score"] is not None
    sentence = scoring.verdict(BASIS, result)
    assert f"fewer than the {scoring.MINIMUM_SCORED}" in sentence
    assert "skill" in sentence


def test_a_sample_with_one_outcome_cannot_win_on_an_infinite_baseline() -> None:
    """Every forecast resolves true. The base-rate baseline is clamped, not allowed to be perfect."""
    result = scoring.score_basis(rows_for([("0.80", "0.05")] * 40))
    assert result["base_rate"] == 1.0
    assert result["baselines"]["base_rate"]["probability"] == 0.95
    assert result["log_score"] is not None
    assert result["baselines"]["base_rate"]["log_score"] is not None
    # The clamped baseline is better than 0.80 here, so the skill is honestly negative.
    assert result["skill_vs_base_rate"] < 0


def test_no_forecasts_at_all_reports_nothing_rather_than_a_flattering_default() -> None:
    result = scoring.summarize([row(None, "0.02")] * 10)
    assert result["by_basis"] == []
    assert result["with_forecast"] == 0
    empty = scoring.score_basis([])
    assert empty["scored"] == 0
    assert empty["brier_score"] is None
    assert empty["skill_vs_base_rate"] is None
    assert empty["insufficient_sample"] is True


def test_the_report_carries_the_scoring_and_states_what_it_cannot_show() -> None:
    from datetime import datetime, timedelta, timezone

    from quant_ai.analytics.decision_quality import summarize

    now = datetime(2026, 9, 22, 4, 0, tzinfo=timezone.utc)
    journal = []
    for index, (probability, forward) in enumerate([("0.95", "0.05"), ("0.05", "-0.05")] * 20):
        journal.append({
            "decision_id": f"d{index}", "tenant_id": "ghost", "symbol": "TRENT",
            "decided_at": (now - timedelta(hours=2, minutes=index)).isoformat(),
            "stance": "BUY", "governance": "abstained", "agents": "{}",
            **row(probability, forward),
        })
    report = summarize(journal, tenant_id="ghost", now=now, since=now - timedelta(days=30))
    section = report["forecast_scoring"]
    assert section["schema"] == scoring.SCHEMA
    assert section["decisions"] == 40
    assert section["by_basis"][0]["basis"] == BASIS
    assert section["by_basis"][0]["skill_vs_base_rate"] > 0.9
    assert any("never pooled" in note for note in section["limitations"])
    assert any("base rate of this sample" in note for note in section["limitations"])
