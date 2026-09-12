from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from quant_ai.backtest.costs import CostModel
from quant_ai.marketdata.models import Candle
from quant_ai.strategies.base import Strategy


@dataclass(frozen=True)
class ReplayResult:
    equity_curve: tuple[Decimal, ...]
    trade_pnls: tuple[Decimal, ...]


class ReplayEngine:
    """Simple close-to-close replay with no future-bar access."""

    def __init__(self, cost_model: CostModel | None = None) -> None:
        self.cost_model = cost_model or CostModel()

    def run(self, candles: tuple[Candle, ...], strategy: Strategy, initial_equity: Decimal) -> ReplayResult:
        if initial_equity <= 0:
            raise ValueError("initial_equity must be positive")
        ordered = tuple(sorted(candles, key=lambda c: c.timestamp))
        equity = initial_equity
        curve = [equity]
        pnls: list[Decimal] = []
        position_side = None
        entry = Decimal(0)
        for idx, candle in enumerate(ordered):
            visible = ordered[: idx + 1]
            signal = strategy.on_bar(visible)
            if position_side is not None:
                raw = (candle.close - entry) if position_side.value == "BUY" else (entry - candle.close)
                notional = entry
                pnl = raw - self.cost_model.one_way_cost(notional) - self.cost_model.one_way_cost(candle.close)
                equity += pnl
                pnls.append(pnl)
                position_side = None
            if signal is not None and idx < len(ordered) - 1:
                position_side = signal.side
                entry = candle.close
            curve.append(equity)
        return ReplayResult(tuple(curve), tuple(pnls))
