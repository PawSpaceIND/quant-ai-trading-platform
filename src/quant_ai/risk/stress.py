from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class StressPosition:
    symbol: str
    notional: Decimal
    beta: Decimal = Decimal(1)


@dataclass(frozen=True)
class StressResult:
    shock: Decimal
    estimated_pnl: Decimal
    estimated_loss_fraction: Decimal


def equity_shock(positions: tuple[StressPosition, ...], equity: Decimal, shock: Decimal) -> StressResult:
    if equity <= 0:
        raise ValueError("equity must be positive")
    pnl = sum((position.notional * position.beta * shock for position in positions), Decimal(0))
    loss_fraction = max(-pnl / equity, Decimal(0))
    return StressResult(shock, pnl, loss_fraction)
