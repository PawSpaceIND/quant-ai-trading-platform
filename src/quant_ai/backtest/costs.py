from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class CostModel:
    commission_bps: Decimal = Decimal(1)
    slippage_bps: Decimal = Decimal(2)
    spread_bps: Decimal = Decimal(1)

    def one_way_cost(self, notional: Decimal) -> Decimal:
        if notional < 0:
            raise ValueError("notional cannot be negative")
        total_bps = self.commission_bps + self.slippage_bps + self.spread_bps
        return notional * total_bps / Decimal(10000)
