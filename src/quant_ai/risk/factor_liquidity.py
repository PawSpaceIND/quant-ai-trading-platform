"""Factor concentration, liquidation horizon and explicit stress scenarios.

No factor loading, volume haircut or execution cost is estimated here. The risk engine uses
only caller-supplied evidence and refuses incomplete projected books instead of interpreting
missing factor/liquidity data as zero risk.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from quant_ai.risk.policy import RiskDecision


@dataclass(frozen=True)
class FactorLiquidityPosition:
    symbol: str
    market_value: Decimal
    quantity: int
    mark_price: Decimal
    factor_loadings: Mapping[str, Decimal]
    average_daily_volume: Decimal
    stress_volume_fraction: Decimal
    stress_execution_cost_fraction: Decimal

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise ValueError("factor_liquidity_symbol_required")
        for name, value in (
            ("market_value", self.market_value),
            ("mark_price", self.mark_price),
            ("average_daily_volume", self.average_daily_volume),
            ("stress_volume_fraction", self.stress_volume_fraction),
            ("stress_execution_cost_fraction", self.stress_execution_cost_fraction),
        ):
            if not value.is_finite():
                raise ValueError(f"{name}_must_be_finite")
        if self.market_value < 0 or self.mark_price <= 0 or self.average_daily_volume <= 0:
            raise ValueError("factor_liquidity_position_values_invalid")
        if type(self.quantity) is not int or self.quantity < 0:
            raise ValueError("factor_liquidity_quantity_must_be_nonnegative_integer")
        if not Decimal(0) < self.stress_volume_fraction <= 1:
            raise ValueError("stress_volume_fraction_must_be_in_0_1")
        if not Decimal(0) <= self.stress_execution_cost_fraction <= 1:
            raise ValueError("stress_execution_cost_fraction_must_be_in_0_1")
        if not self.factor_loadings:
            raise ValueError("factor_loadings_required")
        for factor, loading in self.factor_loadings.items():
            if not factor.strip() or not loading.is_finite():
                raise ValueError("factor_loading_identity_and_value_required")


@dataclass(frozen=True)
class FactorLiquidityPolicy:
    factor_caps: Mapping[str, Decimal]
    max_daily_participation: Decimal = Decimal("0.10")
    max_liquidation_days: Decimal = Decimal(5)
    max_total_liquidation_cost_fraction: Decimal = Decimal("0.02")

    def __post_init__(self) -> None:
        if not self.factor_caps:
            raise ValueError("factor_caps_required")
        for factor, cap in self.factor_caps.items():
            if not factor.strip() or not cap.is_finite() or cap <= 0:
                raise ValueError("factor_cap_identity_and_value_invalid")
        if not Decimal(0) < self.max_daily_participation <= 1:
            raise ValueError("max_daily_participation_must_be_in_0_1")
        if not self.max_liquidation_days.is_finite() or self.max_liquidation_days <= 0:
            raise ValueError("max_liquidation_days_must_be_positive")
        if (
            not self.max_total_liquidation_cost_fraction.is_finite()
            or not Decimal(0) < self.max_total_liquidation_cost_fraction <= 1
        ):
            raise ValueError("max_total_liquidation_cost_fraction_must_be_in_0_1")


@dataclass(frozen=True)
class PositionLiquidityRisk:
    symbol: str
    stressed_daily_capacity: Decimal
    liquidation_days: Decimal
    stressed_execution_cost: Decimal


@dataclass(frozen=True)
class FactorLiquidityMeasure:
    factor_exposure: Mapping[str, Decimal]
    factor_fraction: Mapping[str, Decimal]
    positions: tuple[PositionLiquidityRisk, ...]
    total_stressed_execution_cost: Decimal
    total_stressed_execution_cost_fraction: Decimal


@dataclass(frozen=True)
class FactorScenario:
    name: str
    factor_shocks: Mapping[str, Decimal]
    idiosyncratic_shocks: Mapping[str, Decimal]

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.factor_shocks:
            raise ValueError("factor_scenario_name_and_shocks_required")
        for mapping in (self.factor_shocks, self.idiosyncratic_shocks):
            for name, shock in mapping.items():
                if not name.strip() or not shock.is_finite():
                    raise ValueError("factor_scenario_shock_invalid")


def measure_factor_liquidity(
    positions: tuple[FactorLiquidityPosition, ...],
    *,
    equity: Decimal,
    policy: FactorLiquidityPolicy,
) -> FactorLiquidityMeasure:
    if not equity.is_finite() or equity <= 0:
        raise ValueError("factor_liquidity_equity_must_be_positive")
    if not positions:
        raise ValueError("factor_liquidity_positions_required")
    symbols = [item.symbol for item in positions]
    if len(symbols) != len(set(symbols)):
        raise ValueError("duplicate_factor_liquidity_position")
    required = set(policy.factor_caps)
    factor_exposure = {factor: Decimal(0) for factor in required}
    liquidity: list[PositionLiquidityRisk] = []
    total_cost = Decimal(0)
    for position in positions:
        missing = required - set(position.factor_loadings)
        if missing:
            raise ValueError(
                f"factor_loading_missing:{position.symbol}:{','.join(sorted(missing))}"
            )
        for factor in required:
            factor_exposure[factor] += position.market_value * position.factor_loadings[factor]
        stressed_adv = position.average_daily_volume * position.stress_volume_fraction
        daily_capacity = stressed_adv * policy.max_daily_participation
        if daily_capacity <= 0:
            raise ValueError(f"liquidity_capacity_unavailable:{position.symbol}")
        liquidation_days = Decimal(position.quantity) / daily_capacity
        cost = position.market_value * position.stress_execution_cost_fraction
        total_cost += cost
        liquidity.append(
            PositionLiquidityRisk(position.symbol, daily_capacity, liquidation_days, cost)
        )
    fractions = {factor: value / equity for factor, value in factor_exposure.items()}
    return FactorLiquidityMeasure(
        factor_exposure,
        fractions,
        tuple(liquidity),
        total_cost,
        total_cost / equity,
    )


class FactorLiquidityFirewall:
    def __init__(self, policy: FactorLiquidityPolicy) -> None:
        self.policy = policy

    def evaluate(
        self, positions: tuple[FactorLiquidityPosition, ...], *, equity: Decimal
    ) -> RiskDecision:
        try:
            measure = measure_factor_liquidity(positions, equity=equity, policy=self.policy)
        except (TypeError, ValueError) as error:
            return RiskDecision(False, f"factor_liquidity_measure_unavailable:{error}")
        for factor, fraction in measure.factor_fraction.items():
            if abs(fraction) > self.policy.factor_caps[factor]:
                return RiskDecision(False, f"factor_exposure_limit:{factor}")
        for item in measure.positions:
            if item.liquidation_days > self.policy.max_liquidation_days:
                return RiskDecision(False, f"liquidation_horizon_limit:{item.symbol}")
        if (
            measure.total_stressed_execution_cost_fraction
            > self.policy.max_total_liquidation_cost_fraction
        ):
            return RiskDecision(False, "liquidation_cost_limit")
        return RiskDecision(True, "approved_factor_liquidity")


def scenario_pnl(
    positions: tuple[FactorLiquidityPosition, ...], scenario: FactorScenario
) -> Decimal:
    """Linear factor + explicit idiosyncratic shock P&L, with complete factor coverage."""
    total = Decimal(0)
    required = set(scenario.factor_shocks)
    for position in positions:
        missing = required - set(position.factor_loadings)
        if missing:
            raise ValueError(
                f"scenario_factor_loading_missing:{position.symbol}:{','.join(sorted(missing))}"
            )
        factor_return = sum(
            (
                position.factor_loadings[factor] * scenario.factor_shocks[factor]
                for factor in required
            ),
            Decimal(0),
        )
        idiosyncratic = scenario.idiosyncratic_shocks.get(position.symbol, Decimal(0))
        total += position.market_value * (factor_return + idiosyncratic)
    return total
