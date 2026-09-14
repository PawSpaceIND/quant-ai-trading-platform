from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from quant_ai.domain.models import Side


@dataclass(frozen=True)
class StopPlan:
    initial_stop: Decimal
    take_profit: Decimal
    risk_per_unit: Decimal
    reward_per_unit: Decimal

    @property
    def reward_risk(self) -> Decimal:
        return self.reward_per_unit / self.risk_per_unit if self.risk_per_unit > 0 else Decimal(0)


def atr_stop_plan(
    entry: Decimal,
    side: Side,
    atr: Decimal,
    stop_atr: Decimal = Decimal(2),
    reward_multiple: Decimal = Decimal(2),
) -> StopPlan:
    if min(entry, atr, stop_atr, reward_multiple) <= 0:
        raise ValueError("positive entry/atr/multipliers required")
    risk = atr * stop_atr
    reward = risk * reward_multiple
    if side == Side.BUY:
        return StopPlan(entry - risk, entry + reward, risk, reward)
    return StopPlan(entry + risk, entry - reward, risk, reward)


def trailing_stop(side: Side, peak_or_trough: Decimal, atr: Decimal, multiple: Decimal = Decimal(2)) -> Decimal:
    if min(peak_or_trough, atr, multiple) <= 0:
        raise ValueError("positive values required")
    distance = atr * multiple
    return peak_or_trough - distance if side == Side.BUY else peak_or_trough + distance


def orient_protective_levels(
    side: Side | None,
    reference_price: Decimal,
    stop_price: Decimal | None,
    take_profit_price: Decimal | None,
) -> tuple[Decimal | None, Decimal | None]:
    """Place stop and take-profit on the correct side of ``reference_price``.

    Only the *distance* of each level from the reference is trusted; the side
    decides the direction. For a BUY the stop sits below and the take-profit
    above; for a SELL both are mirrored. Idempotent, so already-correct levels
    pass through unchanged, and a ``None`` side or level is returned as is.
    """
    if side is None or reference_price <= 0:
        return stop_price, take_profit_price
    stop_distance = abs(reference_price - stop_price) if stop_price is not None else None
    profit_distance = abs(take_profit_price - reference_price) if take_profit_price is not None else None
    if side == Side.BUY:
        stop = reference_price - stop_distance if stop_distance is not None else None
        profit = reference_price + profit_distance if profit_distance is not None else None
    else:
        stop = reference_price + stop_distance if stop_distance is not None else None
        profit = reference_price - profit_distance if profit_distance is not None else None
    return stop, profit
