from decimal import Decimal

from quant_ai.domain.models import Opportunity, Side


def stop_and_target(opportunity: Opportunity, price: Decimal) -> tuple[Decimal, Decimal]:
    stop_gap = price * opportunity.stop_distance
    target_gap = price * opportunity.take_profit_distance
    if opportunity.side is Side.BUY:
        return price - stop_gap, price + target_gap
    return price + stop_gap, price - target_gap
