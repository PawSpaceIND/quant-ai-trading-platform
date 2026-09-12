from decimal import Decimal

from quant_ai.derivatives.options import OptionContract, OptionLeg, OptionType
from quant_ai.derivatives.structures import vertical_spread_profile


def test_call_intrinsic_value() -> None:
    contract = OptionContract("AAPL260116C200", "AAPL", OptionType.CALL, Decimal(200), "2026-01-16", Decimal(100), "USD", "OPRA")
    assert contract.intrinsic_value(Decimal(215)) == Decimal(15)


def test_defined_risk_call_debit_spread() -> None:
    long = OptionLeg(OptionContract("L", "AAPL", OptionType.CALL, Decimal(200), "2026-01-16", Decimal(100), "USD", "OPRA"), 1, Decimal(8))
    short = OptionLeg(OptionContract("S", "AAPL", OptionType.CALL, Decimal(210), "2026-01-16", Decimal(100), "USD", "OPRA"), -1, Decimal(4))
    profile = vertical_spread_profile(long, short)
    assert profile.max_loss == Decimal(400)
    assert profile.max_profit == Decimal(600)
