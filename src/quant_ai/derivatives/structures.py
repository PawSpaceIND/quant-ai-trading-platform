from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from quant_ai.derivatives.options import OptionLeg, OptionType


@dataclass(frozen=True)
class DefinedRiskProfile:
    max_loss: Decimal
    max_profit: Decimal | None
    net_premium: Decimal


def vertical_spread_profile(long_leg: OptionLeg, short_leg: OptionLeg) -> DefinedRiskProfile:
    if long_leg.contract.option_type != short_leg.contract.option_type:
        raise ValueError("vertical legs must share option type")
    if long_leg.contract.expiry != short_leg.contract.expiry:
        raise ValueError("vertical legs must share expiry")
    if long_leg.quantity <= 0 or short_leg.quantity >= 0:
        raise ValueError("expected long positive quantity and short negative quantity")
    if abs(long_leg.quantity) != abs(short_leg.quantity):
        raise ValueError("vertical spread requires matched quantities")
    qty = Decimal(abs(long_leg.quantity)) * long_leg.contract.multiplier
    debit = (long_leg.premium - short_leg.premium) * qty
    width = abs(long_leg.contract.strike - short_leg.contract.strike) * qty
    if debit <= 0:
        credit = -debit
        return DefinedRiskProfile(width - credit, credit, -credit)
    return DefinedRiskProfile(debit, width - debit, debit)


def is_bullish_vertical(long_leg: OptionLeg, short_leg: OptionLeg) -> bool:
    if long_leg.contract.option_type == OptionType.CALL:
        return long_leg.contract.strike < short_leg.contract.strike
    return long_leg.contract.strike < short_leg.contract.strike
