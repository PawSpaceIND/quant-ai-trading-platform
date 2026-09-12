from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from quant_ai.domain.models import Side


@dataclass(frozen=True)
class Position:
    symbol: str
    quantity: int
    average_cost: Decimal


@dataclass
class PaperAccount:
    cash: Decimal
    realized_pnl: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        if self.cash < 0:
            raise ValueError("cash cannot be negative")
        self._positions: dict[str, Position] = {}

    def position(self, symbol: str) -> Position | None:
        return self._positions.get(symbol)

    def apply_fill(self, symbol: str, side: Side, quantity: int, price: Decimal, fees: Decimal = Decimal(0)) -> None:
        if quantity <= 0 or price <= 0 or fees < 0:
            raise ValueError("invalid fill")
        current = self._positions.get(symbol, Position(symbol, 0, Decimal(0)))
        if side == Side.BUY:
            cost = price * quantity + fees
            if cost > self.cash:
                raise ValueError("insufficient paper cash")
            new_qty = current.quantity + quantity
            new_avg = ((current.average_cost * current.quantity) + (price * quantity)) / Decimal(new_qty)
            self.cash -= cost
            self._positions[symbol] = Position(symbol, new_qty, new_avg)
            return
        if quantity > current.quantity:
            raise ValueError("paper account cannot sell more than held quantity")
        proceeds = price * quantity - fees
        self.cash += proceeds
        self.realized_pnl += (price - current.average_cost) * quantity - fees
        remaining = current.quantity - quantity
        if remaining == 0:
            self._positions.pop(symbol, None)
        else:
            self._positions[symbol] = Position(symbol, remaining, current.average_cost)

    def unrealized_pnl(self, marks: dict[str, Decimal]) -> Decimal:
        total = Decimal(0)
        for symbol, position in self._positions.items():
            mark = marks.get(symbol)
            if mark is None:
                raise KeyError(f"missing mark for {symbol}")
            total += (mark - position.average_cost) * position.quantity
        return total

    def equity(self, marks: dict[str, Decimal]) -> Decimal:
        market_value = sum((marks[symbol] * position.quantity for symbol, position in self._positions.items()), Decimal(0))
        return self.cash + market_value
