"""Aggregate option Greeks and scenario P&L across signed option positions."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from quant_ai.derivatives.options import Greeks, OptionContract


@dataclass(frozen=True)
class OptionRiskPosition:
    contract: OptionContract
    signed_contracts: int
    greeks_per_unit: Greeks

    def __post_init__(self) -> None:
        if type(self.signed_contracts) is not int or self.signed_contracts == 0:
            raise ValueError("option_risk_position_contracts_must_be_nonzero_integer")


@dataclass(frozen=True)
class OptionRiskExposure:
    delta: Decimal
    gamma: Decimal
    theta: Decimal
    vega: Decimal
    rho: Decimal


def aggregate_option_greeks(positions: tuple[OptionRiskPosition, ...]) -> OptionRiskExposure:
    if not positions:
        return OptionRiskExposure(*(Decimal(0) for _ in range(5)))
    totals = {name: Decimal(0) for name in ("delta", "gamma", "theta", "vega", "rho")}
    for position in positions:
        units = Decimal(position.signed_contracts) * position.contract.multiplier
        for name in totals:
            value = getattr(position.greeks_per_unit, name)
            if not value.is_finite():
                raise ValueError("option_greek_must_be_finite")
            totals[name] += units * value
    return OptionRiskExposure(**totals)


def delta_gamma_scenario_pnl(
    exposure: OptionRiskExposure,
    *,
    underlying_move: Decimal,
    volatility_move: Decimal = Decimal(0),
    days_elapsed: Decimal = Decimal(0),
    rate_move: Decimal = Decimal(0),
) -> Decimal:
    """Second-order local approximation; not a substitute for full repricing stress."""
    for value in (underlying_move, volatility_move, days_elapsed, rate_move):
        if not value.is_finite():
            raise ValueError("option_scenario_input_must_be_finite")
    return (
        exposure.delta * underlying_move
        + Decimal("0.5") * exposure.gamma * underlying_move * underlying_move
        + exposure.vega * volatility_move
        + exposure.theta * days_elapsed
        + exposure.rho * rate_move
    )
