from decimal import Decimal

import pytest

from quant_ai.portfolio.optimizer import (
    PortfolioOptimizationPolicy,
    StrategyOpportunity,
    StrategyPortfolioOptimizer,
)
from quant_ai.portfolio.rebalance import (
    RebalanceAction,
    RebalancePolicy,
    plan_rebalance,
)

D = Decimal


def opportunity(name: str, expected: str = "0.12") -> StrategyOpportunity:
    return StrategyOpportunity(name, D(expected), D("0.20"), D("0.05"), D("0.90"))


def target_result(*names: str):
    items = tuple(opportunity(name) for name in names)
    correlations = {
        (left, right): D("0.1")
        for left in names for right in names if left != right
    }
    policy = PortfolioOptimizationPolicy(
        max_gross_weight=D("0.40"), max_strategy_weight=D("0.20"),
        max_portfolio_volatility=D("0.30"), risk_aversion=D("0.1"),
        turnover_penalty=D(0), weight_step=D("0.01"),
    )
    return StrategyPortfolioOptimizer().optimize(
        items, correlations=correlations, policy=policy
    )


def test_rebalance_orders_decreases_before_increases_and_conserves_cash() -> None:
    result = target_result("trend", "meanrev")
    target = result.weights()
    assert target["trend"] > 0 and target["meanrev"] > 0
    plan = plan_rebalance(
        result,
        current_weights={"legacy": D("0.20"), "trend": D("0.05")},
        policy=RebalancePolicy(deadband_weight=D(0)),
    )
    actions = [item.action for item in plan.actionable]
    assert actions == sorted(
        actions, key={RebalanceAction.DECREASE: 0, RebalanceAction.INCREASE: 1}.get
    )
    assert plan.actionable[0].strategy_id == "legacy"
    assert plan.actionable[0].target_weight == 0
    assert plan.current_cash_weight == D("0.75")
    assert plan.target_cash_weight == result.cash_weight
    assert plan.projected_cash_weight == result.cash_weight


def test_deadband_holds_tiny_drift_and_reports_residual_turnover() -> None:
    result = target_result("trend")
    target = result.weights()["trend"]
    current = target - D("0.003")
    plan = plan_rebalance(
        result,
        current_weights={"trend": current},
        policy=RebalancePolicy(deadband_weight=D("0.005")),
    )
    assert len(plan.instructions) == 1
    row = plan.instructions[0]
    assert row.action is RebalanceAction.HOLD
    assert row.delta_weight == D("0.003")
    assert plan.actionable == ()
    assert plan.residual_deadband_weight == D("0.003")
    assert plan.projected_cash_weight == D(1) - current


def test_strategy_removed_from_target_is_explicit_full_decrease() -> None:
    result = target_result("good")
    plan = plan_rebalance(
        result,
        current_weights={"bad": D("0.15"), "good": D("0.10")},
        policy=RebalancePolicy(deadband_weight=D("0.01")),
    )
    removed = next(item for item in plan.instructions if item.strategy_id == "bad")
    assert removed.action is RebalanceAction.DECREASE
    assert removed.target_weight == 0
    assert removed.delta_weight == D("-0.15")


def test_corrupt_optimizer_result_or_invalid_current_book_refuses() -> None:
    result = target_result("trend")
    with pytest.raises(ValueError, match="current_strategy_weights_exceed_one"):
        plan_rebalance(result, current_weights={"a": D("0.7"), "b": D("0.5")})

    from dataclasses import replace

    corrupted = replace(result, gross_weight=result.gross_weight + D("0.01"))
    with pytest.raises(ValueError, match="optimizer_result_weight_reconciliation_mismatch"):
        plan_rebalance(corrupted, current_weights={})


def test_rebalance_is_instruction_only_and_has_exact_turnover_identity() -> None:
    result = target_result("a", "b")
    plan = plan_rebalance(
        result,
        current_weights={"a": D("0.30"), "old": D("0.10")},
        policy=RebalancePolicy(deadband_weight=D(0)),
    )
    assert plan.residual_deadband_weight == 0
    assert plan.actionable_turnover_weight == plan.desired_turnover_weight
    assert plan.actionable_turnover_weight == sum(
        (abs(item.target_weight - item.current_weight) for item in plan.instructions), D(0)
    )
    assert not hasattr(plan, "submit")
    assert not hasattr(plan, "execute")
