from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class SelectionEvidence:
    """How hard the search looked, and what the data it looked at was made of.

    Every other field on ``StrategyEvidence`` describes how a candidate performed. None of
    them describes how many candidates were tried to find it, and that is the difference
    between a result and a coincidence: the maximum of N draws from a zero-mean distribution
    rises with N, so a best-of-five-hundred sweep clears a profit-factor threshold by
    construction.
    """

    candidate_trials: int
    deflated_sharpe: float
    """P(true sharpe > the best an edgeless search of this size would have produced)."""
    universe_verdict: str
    """From PointInTimeUniverse.audit(); only "plausible" is research-grade."""


@dataclass(frozen=True)
class StrategyEvidence:
    sample_trades: int
    expectancy: Decimal
    max_drawdown: Decimal
    profit_factor: Decimal
    profitable_regimes: int
    paper_days: int


@dataclass(frozen=True)
class PromotionPolicy:
    min_trades: int = 100
    min_expectancy: Decimal = Decimal(0)
    max_drawdown: Decimal = Decimal("0.10")
    min_profit_factor: Decimal = Decimal("1.2")
    min_profitable_regimes: int = 2
    min_paper_days: int = 30
    require_selection_correction: bool = True
    """Defaults to on. Turning it off promotes on statistics uncorrected for the search."""
    min_deflated_sharpe: Decimal = Decimal("0.95")
    """Decimal to match every other threshold on this policy, which the candidate validator
    checks field by field. The statistic itself is a float and is converted at comparison."""


@dataclass(frozen=True)
class PromotionDecision:
    approved: bool
    reasons: tuple[str, ...]


def evaluate_promotion(
    evidence: StrategyEvidence,
    policy: PromotionPolicy | None = None,
    *,
    selection: SelectionEvidence | None = None,
) -> PromotionDecision:
    """Grade a candidate on its performance AND on the search and data that produced it.

    ``selection`` is keyword-only and absent by default, and absence is a rejection rather
    than a pass: a caller that has not measured its selection bias has not shown the
    candidate is anything, and defaulting to approval would make the check decorative.
    """
    policy = policy or PromotionPolicy()
    # Passing SelectionEvidence positionally lands it in `policy` and fails several lines
    # later on a missing attribute, which reads as a bug in the policy rather than a bad
    # call. Refusing here names the actual mistake.
    if not isinstance(policy, PromotionPolicy):
        raise TypeError("promotion policy must be a PromotionPolicy; selection is keyword-only")
    reasons: list[str] = []
    if evidence.sample_trades < policy.min_trades:
        reasons.append("insufficient_trade_sample")
    if evidence.expectancy <= policy.min_expectancy:
        reasons.append("non_positive_expectancy")
    if evidence.max_drawdown > policy.max_drawdown:
        reasons.append("drawdown_too_high")
    if evidence.profit_factor < policy.min_profit_factor:
        reasons.append("profit_factor_too_low")
    if evidence.profitable_regimes < policy.min_profitable_regimes:
        reasons.append("insufficient_regime_coverage")
    if evidence.paper_days < policy.min_paper_days:
        reasons.append("insufficient_paper_history")
    if policy.require_selection_correction:
        if selection is None:
            reasons.append("selection_bias_uncorrected")
        else:
            if selection.candidate_trials < 1:
                reasons.append("candidate_trials_not_recorded")
            if Decimal(str(selection.deflated_sharpe)) < policy.min_deflated_sharpe:
                reasons.append("deflated_sharpe_below_threshold")
            if selection.universe_verdict != "plausible":
                reasons.append("universe_not_research_grade")
    return PromotionDecision(not reasons, tuple(reasons))
