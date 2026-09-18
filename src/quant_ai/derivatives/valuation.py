"""Option valuation and Greeks from explicit market inputs.

The engine uses a Cox-Ross-Rubinstein tree with Decimal arithmetic.  Volatility, rates,
valuation date and exercise style are inputs; nothing is sourced or assumed here.  The
same tree can value European or American exercise and is also used by the bounded implied
volatility solver, keeping the model internally consistent.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from enum import Enum

from quant_ai.derivatives.options import Greeks, OptionContract


class ExerciseStyle(str, Enum):
    EUROPEAN = "EUROPEAN"
    AMERICAN = "AMERICAN"


@dataclass(frozen=True)
class OptionPricingInputs:
    spot: Decimal
    annual_volatility: Decimal
    risk_free_rate: Decimal
    dividend_yield: Decimal
    valuation_date: date
    exercise_style: ExerciseStyle
    steps: int = 200

    def __post_init__(self) -> None:
        for name, value in (
            ("spot", self.spot),
            ("annual_volatility", self.annual_volatility),
            ("risk_free_rate", self.risk_free_rate),
            ("dividend_yield", self.dividend_yield),
        ):
            if not value.is_finite():
                raise ValueError(f"option_{name}_must_be_finite")
        if self.spot <= 0 or self.annual_volatility <= 0:
            raise ValueError("option_spot_and_volatility_must_be_positive")
        if type(self.steps) is not int or not 10 <= self.steps <= 2000:
            raise ValueError("option_tree_steps_must_be_between_10_and_2000")


@dataclass(frozen=True)
class OptionValuation:
    price: Decimal
    intrinsic_value: Decimal
    time_value: Decimal
    model: str
    steps: int


def _expiry(contract: OptionContract) -> date:
    try:
        return date.fromisoformat(contract.expiry)
    except ValueError as error:
        raise ValueError(f"option_expiry_must_be_iso_date:{contract.symbol}") from error


def value_option(contract: OptionContract, inputs: OptionPricingInputs) -> OptionValuation:
    if contract.strike <= 0 or contract.multiplier <= 0:
        raise ValueError("option_contract_strike_and_multiplier_must_be_positive")
    expiry = _expiry(contract)
    days = (expiry - inputs.valuation_date).days
    intrinsic = contract.intrinsic_value(inputs.spot)
    if days <= 0:
        return OptionValuation(intrinsic, intrinsic, Decimal(0), "CRR_EXPIRY_INTRINSIC", 0)
    time = Decimal(days) / Decimal(365)
    dt = time / Decimal(inputs.steps)
    root_dt = dt.sqrt()
    up = (inputs.annual_volatility * root_dt).exp()
    down = Decimal(1) / up
    growth = ((inputs.risk_free_rate - inputs.dividend_yield) * dt).exp()
    denominator = up - down
    if denominator == 0:
        raise ValueError("option_tree_degenerate")
    probability = (growth - down) / denominator
    if not Decimal(0) <= probability <= Decimal(1):
        raise ValueError("option_tree_risk_neutral_probability_out_of_range")
    discount = (-inputs.risk_free_rate * dt).exp()
    values: list[Decimal] = []
    for down_moves in range(inputs.steps + 1):
        node_spot = (
            inputs.spot
            * (up ** (inputs.steps - down_moves))
            * (down ** down_moves)
        )
        values.append(contract.intrinsic_value(node_spot))
    for step in range(inputs.steps - 1, -1, -1):
        next_values: list[Decimal] = []
        for down_moves in range(step + 1):
            continuation = discount * (
                probability * values[down_moves]
                + (Decimal(1) - probability) * values[down_moves + 1]
            )
            if inputs.exercise_style is ExerciseStyle.AMERICAN:
                node_spot = inputs.spot * (up ** (step - down_moves)) * (down ** down_moves)
                continuation = max(continuation, contract.intrinsic_value(node_spot))
            next_values.append(continuation)
        values = next_values
    price = values[0]
    # Numerical models can produce a sub-intrinsic European value when dividend/carry inputs
    # make the early value relationship non-obvious. American exercise must never be below
    # immediate exercise, while European value is left as the model result.
    if inputs.exercise_style is ExerciseStyle.AMERICAN:
        price = max(price, intrinsic)
    return OptionValuation(
        price=price,
        intrinsic_value=intrinsic,
        time_value=price - intrinsic,
        model="CRR",
        steps=inputs.steps,
    )


def implied_volatility(
    contract: OptionContract,
    inputs: OptionPricingInputs,
    *,
    market_premium: Decimal,
    minimum_volatility: Decimal = Decimal("0.0001"),
    maximum_volatility: Decimal = Decimal(5),
    tolerance: Decimal = Decimal("0.000001"),
    max_iterations: int = 100,
) -> Decimal:
    """Solve IV by bisection against the exact configured tree; never extrapolate silently."""
    if not market_premium.is_finite() or market_premium < 0:
        raise ValueError("option_market_premium_must_be_nonnegative_finite")
    if market_premium < contract.intrinsic_value(inputs.spot):
        raise ValueError("option_market_premium_below_intrinsic")
    if not Decimal(0) < minimum_volatility < maximum_volatility:
        raise ValueError("option_iv_bounds_invalid")
    if tolerance <= 0 or max_iterations < 1:
        raise ValueError("option_iv_solver_controls_invalid")
    low, high = minimum_volatility, maximum_volatility
    # With a finite CRR step, an extremely small volatility can make the risk-neutral
    # probability invalid when carry exceeds the first up move. That is a model-domain
    # boundary, not a market quote. Raise the lower search bound until the configured tree
    # is mathematically valid; never clamp the probability into [0,1].
    while low < high:
        try:
            low_price = value_option(
                contract, replace(inputs, annual_volatility=low)
            ).price
        except ValueError as error:
            if str(error) != "option_tree_risk_neutral_probability_out_of_range":
                raise
            low *= Decimal(2)
            continue
        break
    else:
        raise ValueError("option_iv_search_has_no_valid_lower_tree")
    try:
        high_price = value_option(contract, replace(inputs, annual_volatility=high)).price
    except ValueError as error:
        raise ValueError("option_iv_search_has_no_valid_upper_tree") from error
    if market_premium < low_price - tolerance or market_premium > high_price + tolerance:
        raise ValueError("option_market_premium_outside_iv_search_range")
    for _ in range(max_iterations):
        mid = (low + high) / Decimal(2)
        price = value_option(contract, replace(inputs, annual_volatility=mid)).price
        difference = price - market_premium
        if abs(difference) <= tolerance:
            return mid
        if difference < 0:
            low = mid
        else:
            high = mid
    result = (low + high) / Decimal(2)
    if abs(value_option(contract, replace(inputs, annual_volatility=result)).price - market_premium) > tolerance * Decimal(2):
        raise ValueError("option_iv_solver_did_not_converge")
    return result


def option_greeks(
    contract: OptionContract,
    inputs: OptionPricingInputs,
    *,
    spot_bump_fraction: Decimal = Decimal("0.001"),
    volatility_bump: Decimal = Decimal("0.0001"),
    rate_bump: Decimal = Decimal("0.0001"),
) -> Greeks:
    """Finite-difference Greeks using the same tree as price/IV.

    Theta is one calendar-day value decay (`tomorrow - today`). Vega and rho are per 1.0
    absolute change in annual volatility/rate, so callers that display per-1% values must
    divide them explicitly rather than relying on an undocumented convention.
    """
    if spot_bump_fraction <= 0 or volatility_bump <= 0 or rate_bump <= 0:
        raise ValueError("option_greek_bumps_must_be_positive")
    base = value_option(contract, inputs).price
    spot_bump = inputs.spot * spot_bump_fraction
    down_spot = inputs.spot - spot_bump
    if down_spot <= 0:
        raise ValueError("option_spot_bump_crosses_zero")
    up_price = value_option(contract, replace(inputs, spot=inputs.spot + spot_bump)).price
    down_price = value_option(contract, replace(inputs, spot=down_spot)).price
    delta = (up_price - down_price) / (Decimal(2) * spot_bump)
    gamma = (up_price - Decimal(2) * base + down_price) / (spot_bump * spot_bump)
    if inputs.annual_volatility <= volatility_bump:
        raise ValueError("option_volatility_bump_crosses_zero")
    vega_up = value_option(
        contract, replace(inputs, annual_volatility=inputs.annual_volatility + volatility_bump)
    ).price
    vega_down = value_option(
        contract, replace(inputs, annual_volatility=inputs.annual_volatility - volatility_bump)
    ).price
    vega = (vega_up - vega_down) / (Decimal(2) * volatility_bump)
    rho_up = value_option(
        contract, replace(inputs, risk_free_rate=inputs.risk_free_rate + rate_bump)
    ).price
    rho_down = value_option(
        contract, replace(inputs, risk_free_rate=inputs.risk_free_rate - rate_bump)
    ).price
    rho = (rho_up - rho_down) / (Decimal(2) * rate_bump)
    tomorrow = inputs.valuation_date + timedelta(days=1)
    theta = value_option(contract, replace(inputs, valuation_date=tomorrow)).price - base
    return Greeks(delta=delta, gamma=gamma, theta=theta, vega=vega, rho=rho)
