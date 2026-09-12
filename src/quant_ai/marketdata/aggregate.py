from __future__ import annotations

from quant_ai.marketdata.models import Candle


def aggregate_bars(bars: tuple[Candle, ...]) -> Candle:
    if not bars:
        raise ValueError("bars are required")
    first, last = bars[0], bars[-1]
    if any(bar.instrument != first.instrument for bar in bars):
        raise ValueError("all bars must use the same instrument")
    return Candle(
        first.instrument,
        last.timestamp,
        first.open,
        max(bar.high for bar in bars),
        min(bar.low for bar in bars),
        last.close,
        sum(bar.volume for bar in bars),
    )


def aggregate_windows(bars: tuple[Candle, ...], bucket_size: int) -> tuple[Candle, ...]:
    if bucket_size <= 0:
        raise ValueError("bucket_size must be positive")
    output: list[Candle] = []
    for start in range(0, len(bars), bucket_size):
        chunk = bars[start : start + bucket_size]
        if len(chunk) < bucket_size:
            break
        output.append(aggregate_bars(chunk))
    return tuple(output)
