from __future__ import annotations

from decimal import Decimal

from quant_ai.domain.models import Side
from quant_ai.marketdata.models import Candle
from quant_ai.strategies.base import Strategy, StrategySignal


class MomentumStrategy(Strategy):
    strategy_id = "momentum.v1"

    def __init__(self, lookback: int = 3, threshold: Decimal = Decimal("0.01")) -> None:
        if lookback < 1:
            raise ValueError("lookback must be positive")
        self.lookback = lookback
        self.threshold = threshold

    def on_bar(self, history: tuple[Candle, ...]) -> StrategySignal | None:
        if len(history) <= self.lookback:
            return None
        old = history[-1 - self.lookback].close
        current = history[-1].close
        change = (current - old) / old
        if change >= self.threshold:
            return StrategySignal(Side.BUY, min(abs(change) * Decimal(10), Decimal(1)), "positive_momentum")
        if change <= -self.threshold:
            return StrategySignal(Side.SELL, min(abs(change) * Decimal(10), Decimal(1)), "negative_momentum")
        return None
