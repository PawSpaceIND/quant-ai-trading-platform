from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class OptionType(str, Enum):
    CALL = "CALL"
    PUT = "PUT"


@dataclass(frozen=True)
class Greeks:
    delta: Decimal
    gamma: Decimal
    theta: Decimal
    vega: Decimal
    rho: Decimal = Decimal(0)


@dataclass(frozen=True)
class OptionContract:
    symbol: str
    underlying: str
    option_type: OptionType
    strike: Decimal
    expiry: str
    multiplier: Decimal
    currency: str
    exchange: str

    def intrinsic_value(self, spot: Decimal) -> Decimal:
        if self.option_type == OptionType.CALL:
            return max(spot - self.strike, Decimal(0))
        return max(self.strike - spot, Decimal(0))


@dataclass(frozen=True)
class OptionLeg:
    contract: OptionContract
    quantity: int
    premium: Decimal

    @property
    def signed_quantity(self) -> int:
        return self.quantity
