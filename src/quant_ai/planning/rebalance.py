from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from quant_ai.planning.capital import CapitalGoalEngine, CapitalPlan, CapitalPlanRequest


class PlanChange(str, Enum):
    TIGHTEN = "TIGHTEN"
    HOLD = "HOLD"
    RELAX = "RELAX"
    HALT = "HALT"


@dataclass(frozen=True)
class ReplanContext:
    prior_plan: CapitalPlan
    request: CapitalPlanRequest
    realized_return: Decimal
    target_return_to_date: Decimal


@dataclass(frozen=True)
class ReplanDecision:
    plan: CapitalPlan
    change: PlanChange
    reasons: tuple[str, ...]


class AdaptiveCapitalPlanner:
    def __init__(self, engine: CapitalGoalEngine | None = None) -> None:
        self.engine = engine or CapitalGoalEngine()

    def replan(self, context: ReplanContext) -> ReplanDecision:
        candidate = self.engine.recommend(context.request)
        reasons: list[str] = []

        if not candidate.trading_allowed:
            reasons.append("market_or_risk_quality_requires_capital_preservation")
            return ReplanDecision(candidate, PlanChange.HALT, tuple(reasons))

        prior = context.prior_plan
        if context.request.current_drawdown > prior.max_drawdown_fraction * Decimal("0.50"):
            candidate = self._cap_risk(candidate, prior)
            reasons.append("drawdown_worsened:risk_increase_blocked")

        if context.realized_return < context.target_return_to_date:
            candidate = self._cap_risk(candidate, prior)
            reasons.append("target_shortfall_does_not_justify_more_risk")

        if candidate.per_trade_risk_fraction < prior.per_trade_risk_fraction:
            change = PlanChange.TIGHTEN
        elif candidate.per_trade_risk_fraction > prior.per_trade_risk_fraction:
            change = PlanChange.RELAX
        else:
            change = PlanChange.HOLD

        if not reasons:
            reasons.append("replanned_from_current_confidence_volatility_liquidity_and_drawdown")
        return ReplanDecision(candidate, change, tuple(reasons))

    @staticmethod
    def _cap_risk(candidate: CapitalPlan, prior: CapitalPlan) -> CapitalPlan:
        if candidate.per_trade_risk_fraction <= prior.per_trade_risk_fraction:
            return candidate
        ratio = prior.per_trade_risk_fraction / candidate.per_trade_risk_fraction
        return CapitalPlan(
            starting_capital=candidate.starting_capital,
            recommended_mode=candidate.recommended_mode,
            per_trade_risk_fraction=prior.per_trade_risk_fraction,
            per_trade_risk_amount=candidate.per_trade_risk_amount * ratio,
            max_daily_loss_fraction=min(candidate.max_daily_loss_fraction, prior.max_daily_loss_fraction),
            max_daily_loss_amount=min(candidate.max_daily_loss_amount, prior.max_daily_loss_amount),
            max_drawdown_fraction=min(candidate.max_drawdown_fraction, prior.max_drawdown_fraction),
            max_drawdown_amount=min(candidate.max_drawdown_amount, prior.max_drawdown_amount),
            max_position_fraction=min(candidate.max_position_fraction, prior.max_position_fraction),
            max_position_amount=min(candidate.max_position_amount, prior.max_position_amount),
            max_gross_exposure_fraction=min(candidate.max_gross_exposure_fraction, prior.max_gross_exposure_fraction),
            cash_reserve_fraction=max(candidate.cash_reserve_fraction, prior.cash_reserve_fraction),
            stop_loss_fraction=candidate.stop_loss_fraction,
            take_profit_fraction=candidate.take_profit_fraction,
            reward_risk_ratio=candidate.reward_risk_ratio,
            daily_goal=candidate.daily_goal,
            weekly_goal=candidate.weekly_goal,
            monthly_goal=candidate.monthly_goal,
            yearly_goal=candidate.yearly_goal,
            trading_allowed=candidate.trading_allowed,
            rationale=candidate.rationale,
        )
