"""Calibration/performance drift gates for a trained trading candidate."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from quant_ai.learning.outcomes import ForecastPerformance


@dataclass(frozen=True)
class ProbabilityDriftPolicy:
    max_brier_degradation: Decimal = Decimal("0.03")
    max_ece_increase: Decimal = Decimal("0.05")
    max_mean_probability_shift: Decimal = Decimal("0.10")
    require_positive_recent_expectancy: bool = True

    def __post_init__(self) -> None:
        for name in (
            "max_brier_degradation", "max_ece_increase", "max_mean_probability_shift"
        ):
            value = getattr(self, name)
            if not value.is_finite() or value < 0 or value > 1:
                raise ValueError(f"{name}_must_be_in_0_1")


@dataclass(frozen=True)
class ProbabilityDriftDecision:
    healthy: bool
    reasons: tuple[str, ...]


def evaluate_probability_drift(
    reference: ForecastPerformance,
    recent: ForecastPerformance,
    policy: ProbabilityDriftPolicy | None = None,
) -> ProbabilityDriftDecision:
    chosen = policy or ProbabilityDriftPolicy()
    if reference.candidate_id != recent.candidate_id:
        raise ValueError("drift_candidate_identity_mismatch")
    reasons: list[str] = []
    if recent.brier_score - reference.brier_score > chosen.max_brier_degradation:
        reasons.append("brier_score_degraded")
    if (
        recent.expected_calibration_error - reference.expected_calibration_error
        > chosen.max_ece_increase
    ):
        reasons.append("calibration_error_increased")
    if abs(recent.mean_probability - reference.mean_probability) > chosen.max_mean_probability_shift:
        reasons.append("mean_probability_shifted")
    if chosen.require_positive_recent_expectancy and recent.mean_after_cost_return <= 0:
        reasons.append("recent_after_cost_expectancy_not_positive")
    return ProbabilityDriftDecision(not reasons, tuple(reasons))
