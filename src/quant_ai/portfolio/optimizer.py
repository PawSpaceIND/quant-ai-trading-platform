"""Evidence-gated, covariance-aware allocation across strategies.

The optimizer allocates *risk budget*, not confidence theatre. Expected returns must already
be after the strategy's declared trading costs. It then uses a deterministic incremental
mean/variance utility under hard gross, per-strategy, drawdown, evidence, capacity,
portfolio-volatility and turnover constraints. Missing pairwise correlation refuses rather
than silently assuming diversification.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class StrategyOpportunity:
    strategy_id: str
    expected_after_cost_return: Decimal
    volatility: Decimal
    max_drawdown: Decimal
    evidence_score: Decimal
    capacity_weight: Decimal = Decimal(1)
    max_weight: Decimal = Decimal(1)

    def __post_init__(self) -> None:
        if not self.strategy_id.strip():
            raise ValueError("strategy_id_required")
        for name, value in (
            ("expected_after_cost_return", self.expected_after_cost_return),
            ("volatility", self.volatility),
            ("max_drawdown", self.max_drawdown),
            ("evidence_score", self.evidence_score),
            ("capacity_weight", self.capacity_weight),
            ("max_weight", self.max_weight),
        ):
            if not value.is_finite():
                raise ValueError(f"{name}_must_be_finite")
        if self.volatility <= 0:
            raise ValueError("strategy_volatility_must_be_positive")
        if not Decimal(0) <= self.max_drawdown <= 1:
            raise ValueError("strategy_drawdown_must_be_in_0_1")
        if not Decimal(0) <= self.evidence_score <= 1:
            raise ValueError("strategy_evidence_score_must_be_in_0_1")
        if not Decimal(0) <= self.capacity_weight <= 1:
            raise ValueError("strategy_capacity_weight_must_be_in_0_1")
        if not Decimal(0) <= self.max_weight <= 1:
            raise ValueError("strategy_max_weight_must_be_in_0_1")


@dataclass(frozen=True)
class PortfolioOptimizationPolicy:
    max_gross_weight: Decimal = Decimal("0.60")
    max_strategy_weight: Decimal = Decimal("0.20")
    max_portfolio_volatility: Decimal = Decimal("0.20")
    max_strategy_drawdown: Decimal = Decimal("0.10")
    min_evidence_score: Decimal = Decimal("0.60")
    risk_aversion: Decimal = Decimal(4)
    turnover_penalty: Decimal = Decimal("0.02")
    weight_step: Decimal = Decimal("0.01")

    def __post_init__(self) -> None:
        for name in (
            "max_gross_weight",
            "max_strategy_weight",
            "max_portfolio_volatility",
            "max_strategy_drawdown",
            "min_evidence_score",
            "weight_step",
        ):
            value = getattr(self, name)
            if not value.is_finite() or not Decimal(0) < value <= 1:
                raise ValueError(f"{name}_must_be_in_0_1")
        if not self.risk_aversion.is_finite() or self.risk_aversion <= 0:
            raise ValueError("risk_aversion_must_be_positive")
        if not self.turnover_penalty.is_finite() or self.turnover_penalty < 0:
            raise ValueError("turnover_penalty_must_be_nonnegative")


@dataclass(frozen=True)
class StrategyAllocation:
    strategy_id: str
    weight: Decimal
    expected_after_cost_return: Decimal
    evidence_score: Decimal


@dataclass(frozen=True)
class PortfolioOptimizationResult:
    allocations: tuple[StrategyAllocation, ...]
    cash_weight: Decimal
    expected_after_cost_return: Decimal
    portfolio_volatility: Decimal
    gross_weight: Decimal
    turnover: Decimal
    excluded: tuple[tuple[str, str], ...]
    iterations: int

    def weights(self) -> dict[str, Decimal]:
        return {item.strategy_id: item.weight for item in self.allocations}


class StrategyPortfolioOptimizer:
    """Long-only strategy allocator using exact Decimal arithmetic."""

    def optimize(
        self,
        opportunities: tuple[StrategyOpportunity, ...],
        *,
        correlations: Mapping[tuple[str, str], Decimal],
        current_weights: Mapping[str, Decimal] | None = None,
        policy: PortfolioOptimizationPolicy | None = None,
    ) -> PortfolioOptimizationResult:
        chosen = policy or PortfolioOptimizationPolicy()
        current = self._current_weights(current_weights or {})
        by_id = {item.strategy_id: item for item in opportunities}
        if len(by_id) != len(opportunities):
            raise ValueError("duplicate_strategy_opportunity")
        eligible: dict[str, StrategyOpportunity] = {}
        excluded: list[tuple[str, str]] = []
        for item in opportunities:
            reason = self._exclusion(item, chosen)
            if reason is None:
                eligible[item.strategy_id] = item
            else:
                excluded.append((item.strategy_id, reason))
        covariance = self._covariance(tuple(eligible.values()), correlations)
        weights = {name: Decimal(0) for name in eligible}
        caps = {
            name: min(item.capacity_weight, item.max_weight, chosen.max_strategy_weight)
            for name, item in eligible.items()
        }
        iterations = 0
        maximum_iterations = int((chosen.max_gross_weight / chosen.weight_step).to_integral_value()) + len(eligible) + 1
        while sum(weights.values(), Decimal(0)) < chosen.max_gross_weight:
            remaining = chosen.max_gross_weight - sum(weights.values(), Decimal(0))
            delta = min(chosen.weight_step, remaining)
            candidates: list[tuple[Decimal, str, Decimal]] = []
            for strategy_id, item in eligible.items():
                headroom = caps[strategy_id] - weights[strategy_id]
                increment = min(delta, headroom)
                if increment <= 0:
                    continue
                trial = dict(weights)
                trial[strategy_id] += increment
                variance = self._variance(trial, covariance)
                if variance < 0:
                    raise ValueError("portfolio_covariance_not_positive_semidefinite_for_trial")
                volatility = variance.sqrt()
                if volatility > chosen.max_portfolio_volatility:
                    continue
                marginal = self._marginal_utility(
                    strategy_id, increment, item, weights, current, covariance, chosen
                )
                if marginal > 0:
                    candidates.append((marginal, strategy_id, increment))
            if not candidates:
                break
            # Stable ID tie-break makes the result reproducible across processes.
            _, strategy_id, increment = max(candidates, key=lambda row: (row[0], row[1]))
            weights[strategy_id] += increment
            iterations += 1
            if iterations > maximum_iterations:
                raise AssertionError("portfolio_optimizer_iteration_bound_exceeded")
        variance = self._variance(weights, covariance)
        if variance < 0:
            raise ValueError("portfolio_covariance_negative_variance")
        gross = sum(weights.values(), Decimal(0))
        expected = sum(
            (weights[name] * eligible[name].expected_after_cost_return for name in eligible),
            Decimal(0),
        )
        turnover = sum(
            (abs(weights.get(name, Decimal(0)) - value) for name, value in current.items()),
            Decimal(0),
        ) + sum(
            (weight for name, weight in weights.items() if name not in current), Decimal(0)
        )
        allocations = tuple(
            StrategyAllocation(
                name, weight, eligible[name].expected_after_cost_return,
                eligible[name].evidence_score,
            )
            for name, weight in sorted(weights.items())
            if weight > 0
        )
        return PortfolioOptimizationResult(
            allocations=allocations,
            cash_weight=Decimal(1) - gross,
            expected_after_cost_return=expected,
            portfolio_volatility=variance.sqrt(),
            gross_weight=gross,
            turnover=turnover,
            excluded=tuple(sorted(excluded)),
            iterations=iterations,
        )

    @staticmethod
    def _exclusion(
        item: StrategyOpportunity, policy: PortfolioOptimizationPolicy
    ) -> str | None:
        if item.expected_after_cost_return <= 0:
            return "after_cost_expectancy_not_positive"
        if item.evidence_score < policy.min_evidence_score:
            return "evidence_score_below_minimum"
        if item.max_drawdown > policy.max_strategy_drawdown:
            return "strategy_drawdown_above_limit"
        if item.capacity_weight <= 0 or item.max_weight <= 0:
            return "strategy_has_no_capacity"
        return None

    @staticmethod
    def _current_weights(values: Mapping[str, Decimal]) -> dict[str, Decimal]:
        result: dict[str, Decimal] = {}
        for name, value in values.items():
            if not name.strip() or not value.is_finite() or value < 0 or value > 1:
                raise ValueError("invalid_current_strategy_weight")
            result[name] = value
        if sum(result.values(), Decimal(0)) > 1:
            raise ValueError("current_strategy_weights_exceed_one")
        return result

    @staticmethod
    def _covariance(
        items: tuple[StrategyOpportunity, ...],
        correlations: Mapping[tuple[str, str], Decimal],
    ) -> dict[tuple[str, str], Decimal]:
        result: dict[tuple[str, str], Decimal] = {}
        for left in items:
            for right in items:
                if left.strategy_id == right.strategy_id:
                    correlation = Decimal(1)
                else:
                    direct = correlations.get((left.strategy_id, right.strategy_id))
                    reverse = correlations.get((right.strategy_id, left.strategy_id))
                    if direct is None and reverse is None:
                        raise ValueError(
                            f"strategy_correlation_missing:{left.strategy_id}:{right.strategy_id}"
                        )
                    if direct is not None and reverse is not None and direct != reverse:
                        raise ValueError(
                            f"strategy_correlation_asymmetric:{left.strategy_id}:{right.strategy_id}"
                        )
                    correlation = direct if direct is not None else reverse
                    assert correlation is not None
                if not correlation.is_finite() or not Decimal(-1) <= correlation <= Decimal(1):
                    raise ValueError("strategy_correlation_out_of_range")
                result[(left.strategy_id, right.strategy_id)] = (
                    correlation * left.volatility * right.volatility
                )
        return result

    @staticmethod
    def _variance(
        weights: Mapping[str, Decimal],
        covariance: Mapping[tuple[str, str], Decimal],
    ) -> Decimal:
        return sum(
            (
                left_weight
                * right_weight
                * covariance[(left, right)]
                for left, left_weight in weights.items()
                for right, right_weight in weights.items()
            ),
            Decimal(0),
        )

    @staticmethod
    def _marginal_utility(
        strategy_id: str,
        increment: Decimal,
        item: StrategyOpportunity,
        weights: Mapping[str, Decimal],
        current: Mapping[str, Decimal],
        covariance: Mapping[tuple[str, str], Decimal],
        policy: PortfolioOptimizationPolicy,
    ) -> Decimal:
        effective_return = item.expected_after_cost_return * item.evidence_score
        covariance_with_book = sum(
            (
                covariance[(strategy_id, other)] * weight
                for other, weight in weights.items()
            ),
            Decimal(0),
        )
        risk_increment = (
            Decimal(2) * increment * covariance_with_book
            + increment * increment * covariance[(strategy_id, strategy_id)]
        )
        before = abs(weights[strategy_id] - current.get(strategy_id, Decimal(0)))
        after = abs(
            weights[strategy_id] + increment - current.get(strategy_id, Decimal(0))
        )
        turnover_change = after - before
        return (
            effective_return * increment
            - policy.risk_aversion * risk_increment
            - policy.turnover_penalty * turnover_change
        )
