from decimal import Decimal

from quant_ai.domain.models import RiskMode
from quant_ai.planning.capital import CapitalGoalEngine, CapitalPlanRequest


def test_plan_scales_with_any_positive_capital() -> None:
    engine = CapitalGoalEngine()
    small = engine.recommend(CapitalPlanRequest(Decimal(10000), Decimal("0.70"), Decimal("0.20")))
    large = engine.recommend(CapitalPlanRequest(Decimal(1000000), Decimal("0.70"), Decimal("0.20")))
    assert small.recommended_mode == large.recommended_mode == RiskMode.BALANCED
    assert large.per_trade_risk_amount == small.per_trade_risk_amount * Decimal(100)
    assert large.max_daily_loss_amount == small.max_daily_loss_amount * Decimal(100)
    assert not small.daily_goal.mandatory


def test_weak_market_quality_moves_to_preservation_mode() -> None:
    plan = CapitalGoalEngine().recommend(CapitalPlanRequest(
        Decimal(500000), Decimal("0.40"), Decimal("0.55"), current_drawdown=Decimal("0.08"), liquidity_score=Decimal("0.45")
    ))
    assert plan.recommended_mode == RiskMode.CONSERVATIVE
    assert not plan.trading_allowed
    assert plan.yearly_goal.target == 0
    assert "capital_preservation_mode:no_trade_until_quality_recovers" in plan.rationale


def test_high_quality_setup_can_recommend_aggressive_mode() -> None:
    plan = CapitalGoalEngine().recommend(CapitalPlanRequest(
        Decimal(250000), Decimal("0.85"), Decimal("0.18"), expected_edge=Decimal("0.02"), liquidity_score=Decimal("0.95")
    ))
    assert plan.recommended_mode == RiskMode.AGGRESSIVE
    assert plan.reward_risk_ratio == Decimal("2.25")
    assert plan.take_profit_fraction > plan.stop_loss_fraction


def test_position_quantity_respects_risk_and_notional_caps() -> None:
    plan = CapitalGoalEngine().recommend(CapitalPlanRequest(Decimal(100000), Decimal("0.70"), Decimal("0.20")))
    quantity = plan.quantity_for_price(Decimal(1000))
    assert quantity > 0
    assert Decimal(quantity) * Decimal(1000) <= plan.max_position_amount


def test_user_can_request_explicit_risk_mode_without_forcing_trade() -> None:
    plan = CapitalGoalEngine().recommend(CapitalPlanRequest(
        Decimal(100000), Decimal("0.30"), Decimal("0.15"), requested_mode=RiskMode.AGGRESSIVE
    ))
    assert plan.recommended_mode == RiskMode.AGGRESSIVE
    assert not plan.trading_allowed
