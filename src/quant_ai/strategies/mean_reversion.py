from __future__ import annotations

from decimal import Decimal

from quant_ai.domain.models import Side
from quant_ai.marketdata.models import Candle
from quant_ai.strategies.base import StrategySignal


class MeanReversionStrategy:
    strategy_id = "mean_reversion"

    def __init__(self, lookback: int = 20, deviation: Decimal = Decimal("0.03")) -> None:
        self.lookback = lookback
        self.deviation = deviation

    def on_bar(self, history: tuple[Candle, ...]) -> StrategySignal | None:
        if len(history) < self.lookback:
            return None
        window = history[-self.lookback:]
        average = sum(bar.close for bar in window) / Decimal(len(window))
        current = history[-1].close
        delta = (current - average) / average
        if delta <= -self.deviation:
            return StrategySignal(Side.BUY, Decimal("0.60"), "oversold_reversion")
        if delta >= self.deviation:
            return StrategySignal(Side.SELL, Decimal("0.60"), "overbought_reversion")
        return None
