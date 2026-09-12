from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


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


@dataclass(frozen=True)
class PromotionDecision:
    approved: bool
    reasons: tuple[str, ...]


def evaluate_promotion(evidence: StrategyEvidence, policy: PromotionPolicy | None = None) -> PromotionDecision:
    policy = policy or PromotionPolicy()
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
    return PromotionDecision(not reasons, tuple(reasons))
