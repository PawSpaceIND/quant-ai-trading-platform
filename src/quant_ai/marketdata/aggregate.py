from __future__ import annotations

from decimal import Decimal

from quant_ai.marketdata.models import Candle


def aggregate_bars(bars: tuple[Candle, ...]) -> Candle:
    if not bars:
        raise ValueError("bars are required")
    instrument = bars[0].instrument
    if any(bar.instrument != instrument for bar in bars):
        raise ValueError("all bars must share instrument")
    ordered = tuple(sorted(bars, key=lambda item: item.timestamp))
    return Candle(
        instrument,
        ordered[-1].timestamp,
        ordered[0].open,
        max(bar.high for bar in ordered),
        min(bar.low for bar in ordered),
        ordered[-1].close,
        sum((bar.volume for bar in ordered), Decimal(0)),
    )
