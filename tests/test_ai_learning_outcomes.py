from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant_ai.learning.drift import evaluate_probability_drift
from quant_ai.learning.outcomes import ForecastOutcomeJournal, ProbabilityForecast

D = Decimal
NOW = datetime(2026, 9, 16, 10, tzinfo=timezone.utc)
H = "a" * 64


def forecast(candidate, index, probability, *, pair=None, regime="NORMAL"):
    return ProbabilityForecast(
        f"{candidate}-{index}", pair or f"pair-{index}", candidate, "INFY", D(probability),
        NOW + timedelta(minutes=index), NOW + timedelta(minutes=index + 1),
        H, H, H, regime,
    )


def populate(journal, candidate, probabilities, outcomes, *, regime="NORMAL"):
    for index, (probability, after_cost) in enumerate(zip(probabilities, outcomes)):
        row = forecast(candidate, index, probability, regime=regime)
        journal.record_forecast(row)
        # gross - cost = requested after-cost return.
        cost = D("0.001")
        journal.resolve(
            row.forecast_id, gross_return=D(after_cost) + cost, cost_return=cost,
            resolved_at=row.resolve_after,
        )


def test_forecast_cannot_resolve_before_horizon_and_costs_define_the_binary_target(tmp_path):
    with ForecastOutcomeJournal(tmp_path / "outcomes.sqlite") as journal:
        row = forecast("candidate", 0, "0.8")
        journal.record_forecast(row)
        with pytest.raises(ValueError, match="before_horizon"):
            journal.resolve(
                row.forecast_id, gross_return=D("0.01"), cost_return=D("0.001"),
                resolved_at=row.decision_at,
            )
        result = journal.resolve(
            row.forecast_id, gross_return=D("0.01"), cost_return=D("0.02"),
            resolved_at=row.resolve_after,
        )
        assert result.after_cost_return == D("-0.01")
        assert not result.positive_after_cost


def test_performance_scores_probability_against_after_cost_outcomes(tmp_path):
    with ForecastOutcomeJournal(tmp_path / "outcomes.sqlite") as journal:
        populate(
            journal, "good", ["0.9", "0.8", "0.2", "0.1"],
            ["0.02", "0.01", "-0.01", "-0.02"],
        )
        report = journal.performance("good", min_samples=4)
        assert report.samples == 4
        assert report.brier_score == D("0.025")
        assert report.positive_rate == D("0.5")
        assert report.mean_after_cost_return == 0
        assert report.total_cost_return == D("0.004")
        assert report.bins


def test_paired_skill_requires_same_realised_outcome_and_rewards_better_calibration(tmp_path):
    with ForecastOutcomeJournal(tmp_path / "outcomes.sqlite") as journal:
        actual = ["0.02", "-0.01", "0.03", "-0.02"]
        populate(journal, "candidate", ["0.9", "0.2", "0.8", "0.1"], actual)
        populate(journal, "baseline", ["0.6", "0.5", "0.6", "0.5"], actual)
        skill = journal.paired_skill("candidate", "baseline", min_pairs=4)
        assert skill.brier_improvement > 0
        # Rewrite one baseline outcome is blocked by append-only identity, so a comparison
        # cannot quietly use a different realised truth for the same pair.
        with pytest.raises(ValueError, match="outcome_payload_mismatch"):
            journal.resolve(
                "baseline-0", gross_return=D("0.5"), cost_return=D(0),
                resolved_at=NOW + timedelta(minutes=1),
            )


def test_forecast_and_outcome_ids_are_idempotent_not_mutable(tmp_path):
    with ForecastOutcomeJournal(tmp_path / "outcomes.sqlite") as journal:
        row = forecast("candidate", 0, "0.7")
        journal.record_forecast(row)
        journal.record_forecast(row)
        with pytest.raises(ValueError, match="forecast_id_payload_mismatch"):
            journal.record_forecast(forecast("candidate", 0, "0.8"))
        journal.resolve(
            row.forecast_id, gross_return=D("0.02"), cost_return=D("0.001"),
            resolved_at=row.resolve_after,
        )
        with pytest.raises(Exception, match="append-only"):
            journal.db.execute("DELETE FROM probability_forecasts")


def test_drift_flags_probability_calibration_and_after_cost_deterioration(tmp_path):
    with ForecastOutcomeJournal(tmp_path / "outcomes.sqlite") as reference_journal:
        populate(reference_journal, "candidate", ["0.8"] * 20 + ["0.2"] * 20,
                 ["0.01"] * 20 + ["-0.01"] * 20)
        reference = reference_journal.performance("candidate", min_samples=40)
    with ForecastOutcomeJournal(tmp_path / "recent.sqlite") as recent_journal:
        populate(recent_journal, "candidate", ["0.9"] * 40, ["-0.01"] * 40)
        recent = recent_journal.performance("candidate", min_samples=40)
    decision = evaluate_probability_drift(reference, recent)
    assert not decision.healthy
    assert "brier_score_degraded" in decision.reasons
    assert "recent_after_cost_expectancy_not_positive" in decision.reasons


def test_promotion_candidate_evaluation_is_built_from_paired_after_cost_outcomes(tmp_path):
    from quant_ai.learning.outcomes import candidate_evaluation_from_outcomes

    with ForecastOutcomeJournal(tmp_path / "outcomes.sqlite") as journal:
        actual = ["0.02", "-0.01"] * 20
        populate(journal, "candidate", ["0.8", "0.2"] * 20, actual)
        populate(journal, "baseline", ["0.55", "0.45"] * 20, actual)
        evaluation = candidate_evaluation_from_outcomes(
            journal, candidate_id="candidate", baseline_id="baseline",
            max_drawdown=D("0.07"), holdout_sha256=H, forward_paper_sha256=H,
            calibration_sha256=H, execution_stress_sha256=H,
            trial_register_sha256=H, min_samples=40,
        )
        assert evaluation.resolved_probability_count == 40
        assert evaluation.brier_score < evaluation.baseline_brier_score
        assert evaluation.after_cost_expectancy == D("0.005")
