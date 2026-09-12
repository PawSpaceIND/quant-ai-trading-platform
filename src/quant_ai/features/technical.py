from __future__ import annotations

from decimal import Decimal

from quant_ai.marketdata.models import Candle


def simple_return(history: tuple[Candle, ...], lookback: int = 1) -> Decimal:
    if len(history) <= lookback or lookback < 1:
        return Decimal(0)
    old = history[-1 - lookback].close
    return (history[-1].close - old) / old


def average_true_range(history: tuple[Candle, ...], lookback: int = 14) -> Decimal:
    if len(history) < 2 or lookback < 1:
        return Decimal(0)
    window = history[-min(len(history), lookback + 1):]
    ranges: list[Decimal] = []
    for previous, current in zip(window, window[1:]):
        ranges.append(max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        ))
    return sum(ranges, Decimal(0)) / Decimal(len(ranges)) if ranges else Decimal(0)


def rolling_volatility(history: tuple[Candle, ...], lookback: int = 20) -> Decimal:
    if len(history) <= 1:
        return Decimal(0)
    closes = history[-min(len(history), lookback):]
    returns = [(b.close - a.close) / a.close for a, b in zip(closes, closes[1:])]
    if not returns:
        return Decimal(0)
    mean = sum(returns, Decimal(0)) / Decimal(len(returns))
    variance = sum((item - mean) ** 2 for item in returns) / Decimal(len(returns))
    return variance.sqrt()
