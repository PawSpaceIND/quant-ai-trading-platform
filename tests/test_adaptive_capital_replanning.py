from decimal import Decimal

from quant_ai.planning.capital import CapitalGoalEngine, CapitalPlanRequest
from quant_ai.planning.rebalance import AdaptiveCapitalPlanner, PlanChange, ReplanContext


def test_target_shortfall_never_increases_risk() -> None:
    engine = CapitalGoalEngine()
    prior = engine.recommend(CapitalPlanRequest(Decimal(100000), Decimal("0.70"), Decimal("0.20")))
    request = CapitalPlanRequest(
        Decimal(100000), Decimal("0.90"), Decimal("0.18"), expected_edge=Decimal("0.03"), liquidity_score=Decimal("0.95")
    )
    decision = AdaptiveCapitalPlanner(engine).replan(ReplanContext(prior, request, Decimal("0.001"), Decimal("0.01")))
    assert decision.plan.per_trade_risk_fraction <= prior.per_trade_risk_fraction
    assert "target_shortfall_does_not_justify_more_risk" in decision.reasons
    assert decision.change in {PlanChange.HOLD, PlanChange.TIGHTEN}


def test_worsening_drawdown_blocks_risk_relaxation() -> None:
    engine = CapitalGoalEngine()
    prior = engine.recommend(CapitalPlanRequest(Decimal(100000), Decimal("0.70"), Decimal("0.20")))
    request = CapitalPlanRequest(
        Decimal(100000), Decimal("0.90"), Decimal("0.18"), expected_edge=Decimal("0.03"),
        current_drawdown=Decimal("0.055"), liquidity_score=Decimal("0.95")
    )
    decision = AdaptiveCapitalPlanner(engine).replan(ReplanContext(prior, request, Decimal("0.02"), Decimal("0.01")))
    assert decision.plan.per_trade_risk_fraction <= prior.per_trade_risk_fraction
    assert "drawdown_worsened:risk_increase_blocked" in decision.reasons


def test_improving_conditions_can_relax_when_not_chasing_targets() -> None:
    engine = CapitalGoalEngine()
    prior = engine.recommend(CapitalPlanRequest(Decimal(100000), Decimal("0.58"), Decimal("0.30")))
    request = CapitalPlanRequest(
        Decimal(100000), Decimal("0.90"), Decimal("0.18"), expected_edge=Decimal("0.03"), liquidity_score=Decimal("0.95")
    )
    decision = AdaptiveCapitalPlanner(engine).replan(ReplanContext(prior, request, Decimal("0.02"), Decimal("0.01")))
    assert decision.change == PlanChange.RELAX
    assert decision.plan.per_trade_risk_fraction > prior.per_trade_risk_fraction


def test_bad_conditions_halt_trading() -> None:
    engine = CapitalGoalEngine()
    prior = engine.recommend(CapitalPlanRequest(Decimal(100000), Decimal("0.70"), Decimal("0.20")))
    request = CapitalPlanRequest(
        Decimal(100000), Decimal("0.30"), Decimal("0.60"), current_drawdown=Decimal("0.09"), liquidity_score=Decimal("0.40")
    )
    decision = AdaptiveCapitalPlanner(engine).replan(ReplanContext(prior, request, Decimal("-0.02"), Decimal("0.01")))
    assert decision.change == PlanChange.HALT
    assert decision.plan.trading_allowed is False
