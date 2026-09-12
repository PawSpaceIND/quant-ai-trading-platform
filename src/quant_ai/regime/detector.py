from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from quant_ai.marketdata.models import Candle


class Regime(str, Enum):
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE = "RANGE"
    HIGH_VOL = "HIGH_VOL"


@dataclass(frozen=True)
class RegimeSnapshot:
    regime: Regime
    trend_return: Decimal
    realized_range: Decimal


class RegimeDetector:
    def detect(self, history: tuple[Candle, ...], lookback: int = 20) -> RegimeSnapshot:
        if len(history) < max(3, lookback):
            return RegimeSnapshot(Regime.RANGE, Decimal(0), Decimal(0))
        window = history[-lookback:]
        start = window[0].close
        end = window[-1].close
        trend_return = (end - start) / start
        average_range = sum((bar.high - bar.low) / bar.close for bar in window) / Decimal(len(window))
        if average_range >= Decimal("0.03"):
            regime = Regime.HIGH_VOL
        elif trend_return >= Decimal("0.04"):
            regime = Regime.TREND_UP
        elif trend_return <= Decimal("-0.04"):
            regime = Regime.TREND_DOWN
        else:
            regime = Regime.RANGE
        return RegimeSnapshot(regime, trend_return, average_range)
