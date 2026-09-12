from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class AllocationInput:
    symbol: str
    score: Decimal
    volatility: Decimal


class RiskParityAllocator:
    def allocate(self, items: tuple[AllocationInput, ...]) -> dict[str, Decimal]:
        eligible = [item for item in items if item.score > 0 and item.volatility > 0]
        if not eligible:
            return {}
        raw = {item.symbol: item.score / item.volatility for item in eligible}
        total = sum(raw.values(), Decimal(0))
        return {symbol: weight / total for symbol, weight in raw.items()}
