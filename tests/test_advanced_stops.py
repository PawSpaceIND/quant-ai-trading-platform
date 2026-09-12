from decimal import Decimal

from quant_ai.domain.models import Side
from quant_ai.risk.stops import atr_stop_plan, trailing_stop


def test_atr_stop_plan_has_two_to_one_reward_risk() -> None:
    plan = atr_stop_plan(Decimal(100), Side.BUY, Decimal(2))
    assert plan.initial_stop == Decimal(96)
    assert plan.take_profit == Decimal(108)
    assert plan.reward_risk == Decimal(2)


def test_trailing_stop_moves_with_peak() -> None:
    assert trailing_stop(Side.BUY, Decimal(120), Decimal(3)) == Decimal(114)
