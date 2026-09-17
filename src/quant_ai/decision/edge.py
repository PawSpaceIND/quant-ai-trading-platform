"""Conservative after-cost edge gate for calibrated trading probabilities.

The gate translates probability quality and payoff geometry into a risk-bounded economic
decision. It does not create probabilities or estimate costs; those are evidence inputs.
Probability is deliberately haircutted for calibration error and uncertainty before any
expected-value or Kelly-style sizing is allowed.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class EdgeEvidence:
    calibrated_probability: Decimal
    calibration_error: Decimal
    probability_uncertainty: Decimal
    average_win_return: Decimal
    average_loss_return: Decimal
    expected_cost_return: Decimal
    resolved_samples: int
    source_sha256: str

    def __post_init__(self) -> None:
        for name in (
            "calibrated_probability", "calibration_error", "probability_uncertainty",
        ):
            value = getattr(self, name)
            if not value.is_finite() or not Decimal(0) <= value <= Decimal(1):
                raise ValueError(f"{name}_must_be_in_0_1")
        for name in ("average_win_return", "average_loss_return"):
            value = getattr(self, name)
            if not value.is_finite() or value <= 0:
                raise ValueError(f"{name}_must_be_positive")
        if not self.expected_cost_return.is_finite() or self.expected_cost_return < 0:
            raise ValueError("expected_cost_return_must_be_nonnegative")
        if type(self.resolved_samples) is not int or self.resolved_samples < 0:
            raise ValueError("resolved_samples_must_be_nonnegative_integer")
        if len(self.source_sha256) != 64 or any(ch not in "0123456789abcdef" for ch in self.source_sha256):
            raise ValueError("edge_source_sha256_invalid")


@dataclass(frozen=True)
class EdgePolicy:
    min_resolved_samples: int = 30
    max_calibration_error: Decimal = Decimal("0.10")
    max_probability_uncertainty: Decimal = Decimal("0.10")
    min_conservative_edge: Decimal = Decimal("0.001")
    kelly_fraction: Decimal = Decimal("0.25")
    max_risk_fraction: Decimal = Decimal("0.02")

    def __post_init__(self) -> None:
        if type(self.min_resolved_samples) is not int or self.min_resolved_samples < 1:
            raise ValueError("min_resolved_samples_must_be_positive_integer")
        for name in (
            "max_calibration_error", "max_probability_uncertainty", "kelly_fraction",
            "max_risk_fraction",
        ):
            value = getattr(self, name)
            if not value.is_finite() or not Decimal(0) < value <= Decimal(1):
                raise ValueError(f"{name}_must_be_in_0_1")
        if not self.min_conservative_edge.is_finite() or self.min_conservative_edge < 0:
            raise ValueError("min_conservative_edge_must_be_nonnegative")


@dataclass(frozen=True)
class EdgeDecision:
    approved: bool
    reasons: tuple[str, ...]
    conservative_probability: Decimal
    break_even_probability: Decimal
    expected_after_cost_return: Decimal
    raw_kelly_fraction: Decimal
    recommended_risk_fraction: Decimal


class CalibratedEdgeGate:
    def __init__(self, policy: EdgePolicy | None = None) -> None:
        self.policy = policy or EdgePolicy()

    def evaluate(self, evidence: EdgeEvidence) -> EdgeDecision:
        p = evidence.calibrated_probability
        conservative = max(
            Decimal(0), p - evidence.calibration_error - evidence.probability_uncertainty
        )
        win = evidence.average_win_return
        loss = evidence.average_loss_return
        cost = evidence.expected_cost_return
        break_even = (loss + cost) / (win + loss)
        expectancy = conservative * win - (Decimal(1) - conservative) * loss - cost
        # Costs reduce a winning payoff and increase a losing payoff. This is
        # a loss-budget fraction, not a fraction of cash to invest at full notional.
        net_win, net_loss = win - cost, loss + cost
        raw_kelly = (max(Decimal(0), conservative - (Decimal(1) - conservative)
                         * net_loss / net_win) if net_win > 0 else Decimal(0))
        recommended = min(
            self.policy.max_risk_fraction,
            raw_kelly * self.policy.kelly_fraction,
        )
        reasons: list[str] = []
        if evidence.resolved_samples < self.policy.min_resolved_samples:
            reasons.append("edge_sample_too_small")
        if evidence.calibration_error > self.policy.max_calibration_error:
            reasons.append("edge_calibration_error_too_high")
        if evidence.probability_uncertainty > self.policy.max_probability_uncertainty:
            reasons.append("edge_probability_uncertainty_too_high")
        if conservative <= break_even:
            reasons.append("edge_probability_not_above_break_even")
        if expectancy < self.policy.min_conservative_edge:
            reasons.append("edge_after_cost_expectancy_below_minimum")
        if recommended <= 0:
            reasons.append("edge_kelly_fraction_not_positive")
        return EdgeDecision(
            approved=not reasons,
            reasons=tuple(reasons),
            conservative_probability=conservative,
            break_even_probability=break_even,
            expected_after_cost_return=expectancy,
            raw_kelly_fraction=raw_kelly,
            recommended_risk_fraction=recommended if not reasons else Decimal(0),
        )
