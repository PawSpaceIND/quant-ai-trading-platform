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
    """One-bar replay: close-derived signals enter only at the next bar open."""

    def __init__(self, cost_model: CostModel | None = None) -> None:
        self.cost_model = cost_model or CostModel()

    def run(
        self,
        candles: tuple[Candle, ...],
        strategy: Strategy,
        initial_equity: Decimal,
    ) -> ReplayResult:
        if initial_equity <= 0:
            raise ValueError("initial_equity must be positive")
        ordered = tuple(sorted(candles, key=lambda c: c.timestamp))
        equity = initial_equity
        curve = [equity]
        pnls: list[Decimal] = []
        pending_side = None
        for idx, candle in enumerate(ordered):
            if pending_side is not None:
                entry = candle.open
                raw = (
                    candle.close - entry
                    if pending_side.value == "BUY"
                    else entry - candle.close
                )
                pnl = (
                    raw
                    - self.cost_model.one_way_cost(entry)
                    - self.cost_model.one_way_cost(candle.close)
                )
                equity += pnl
                pnls.append(pnl)
                pending_side = None

            # The current completed bar can create only a pending order for the next bar.
            visible = ordered[: idx + 1]
            signal = strategy.on_bar(visible)
            if signal is not None and idx < len(ordered) - 1:
                pending_side = signal.side
            curve.append(equity)
        return ReplayResult(tuple(curve), tuple(pnls))
