from __future__ import annotations

from decimal import Decimal

from quant_ai.domain.models import Side
from quant_ai.marketdata.models import Candle
from quant_ai.strategies.base import StrategySignal


class BreakoutStrategy:
    strategy_id = "breakout"

    def __init__(self, lookback: int = 20, buffer: Decimal = Decimal("0.001")) -> None:
        self.lookback = lookback
        self.buffer = buffer

    def on_bar(self, history: tuple[Candle, ...]) -> StrategySignal | None:
        if len(history) <= self.lookback:
            return None
        previous = history[-self.lookback - 1 : -1]
        current = history[-1]
        prior_high = max(bar.high for bar in previous)
        prior_low = min(bar.low for bar in previous)
        if current.close > prior_high * (Decimal(1) + self.buffer):
            return StrategySignal(Side.BUY, Decimal("0.65"), "upside_breakout")
        if current.close < prior_low * (Decimal(1) - self.buffer):
            return StrategySignal(Side.SELL, Decimal("0.65"), "downside_breakout")
        return None
