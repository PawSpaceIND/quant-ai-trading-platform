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
