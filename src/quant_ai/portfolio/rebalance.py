"""Deterministic rebalance instructions derived from an approved strategy target.

The rebalance layer never places an order. It translates current strategy weights and the
optimizer's target into an auditable reduce-first plan while preserving small deviations
inside an explicit deadband. Any execution of an INCREASE remains downstream of risk,
execution planning and OMS.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from quant_ai.portfolio.optimizer import PortfolioOptimizationResult


class RebalanceAction(str, Enum):
    DECREASE = "DECREASE"
    INCREASE = "INCREASE"
    HOLD = "HOLD"


@dataclass(frozen=True)
class RebalancePolicy:
    deadband_weight: Decimal = Decimal("0.005")

    def __post_init__(self) -> None:
        if not self.deadband_weight.is_finite() or not Decimal(0) <= self.deadband_weight < 1:
            raise ValueError("rebalance_deadband_weight_must_be_in_0_1")

@dataclass(frozen=True)
class StrategyRebalanceInstruction:
    strategy_id: str
    current_weight: Decimal
    target_weight: Decimal
    delta_weight: Decimal
    action: RebalanceAction

    @property
    def actionable(self) -> bool:
        return self.action is not RebalanceAction.HOLD


@dataclass(frozen=True)
class PortfolioRebalancePlan:
    instructions: tuple[StrategyRebalanceInstruction, ...]
    current_cash_weight: Decimal
    target_cash_weight: Decimal
    projected_cash_weight: Decimal
    desired_turnover_weight: Decimal
    actionable_turnover_weight: Decimal
    residual_deadband_weight: Decimal

    @property
    def actionable(self) -> tuple[StrategyRebalanceInstruction, ...]:
        return tuple(item for item in self.instructions if item.actionable)

    @property
    def holds(self) -> tuple[StrategyRebalanceInstruction, ...]:
        return tuple(item for item in self.instructions if not item.actionable)


def _weights(values: Mapping[str, Decimal], *, label: str) -> dict[str, Decimal]:
    result: dict[str, Decimal] = {}
    for strategy_id, weight in values.items():
        if not strategy_id.strip() or not weight.is_finite() or not Decimal(0) <= weight <= 1:
            raise ValueError(f"invalid_{label}_strategy_weight")
        result[strategy_id] = weight
    if sum(result.values(), Decimal(0)) > 1:
        raise ValueError(f"{label}_strategy_weights_exceed_one")
    return result


def _target_weights(result: PortfolioOptimizationResult) -> dict[str, Decimal]:
    target = {item.strategy_id: item.weight for item in result.allocations}
    if len(target) != len(result.allocations):
        raise ValueError("duplicate_target_strategy_allocation")
    target = _weights(target, label="target")
    gross = sum(target.values(), Decimal(0))
    if gross != result.gross_weight or result.cash_weight != Decimal(1) - gross:
        raise ValueError("optimizer_result_weight_reconciliation_mismatch")
    if not result.cash_weight.is_finite() or not Decimal(0) <= result.cash_weight <= 1:
        raise ValueError("optimizer_result_cash_weight_invalid")
    return target


def plan_rebalance(
    result: PortfolioOptimizationResult,
    *,
    current_weights: Mapping[str, Decimal],
    policy: RebalancePolicy | None = None,
) -> PortfolioRebalancePlan:
    chosen = policy or RebalancePolicy()
    current = _weights(current_weights, label="current")
    target = _target_weights(result)
    rows: list[StrategyRebalanceInstruction] = []
    for strategy_id in sorted(set(current) | set(target)):
        current_weight = current.get(strategy_id, Decimal(0))
        target_weight = target.get(strategy_id, Decimal(0))
        delta = target_weight - current_weight
        if abs(delta) <= chosen.deadband_weight:
            action = RebalanceAction.HOLD
        elif delta < 0:
            action = RebalanceAction.DECREASE
        else:
            action = RebalanceAction.INCREASE
        rows.append(
            StrategyRebalanceInstruction(
                strategy_id, current_weight, target_weight, delta, action
            )
        )

    # Reductions are deliberately listed before additions. This is an instruction order,
    # not execution authority: downstream risk/OMS still owns every actual trade.
    order = {
        RebalanceAction.DECREASE: 0,
        RebalanceAction.INCREASE: 1,
        RebalanceAction.HOLD: 2,
    }
    rows.sort(key=lambda item: (order[item.action], item.strategy_id))
    projected = {
        item.strategy_id: (
            item.current_weight if item.action is RebalanceAction.HOLD else item.target_weight
        )
        for item in rows
    }
    desired_turnover = sum((abs(item.delta_weight) for item in rows), Decimal(0))
    actionable_turnover = sum(
        (abs(item.delta_weight) for item in rows if item.actionable), Decimal(0)
    )
    residual = desired_turnover - actionable_turnover
    current_cash = Decimal(1) - sum(current.values(), Decimal(0))
    projected_cash = Decimal(1) - sum(projected.values(), Decimal(0))
    if projected_cash < 0:
        raise ValueError("rebalance_projected_cash_negative")
    return PortfolioRebalancePlan(
        tuple(rows), current_cash, result.cash_weight, projected_cash,
        desired_turnover, actionable_turnover, residual,
    )
