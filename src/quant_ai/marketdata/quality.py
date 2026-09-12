from __future__ import annotations

from dataclasses import dataclass

from quant_ai.marketdata.models import Candle


@dataclass(frozen=True)
class DataQualityResult:
    valid: bool
    reasons: tuple[str, ...]


def validate_series(bars: tuple[Candle, ...]) -> DataQualityResult:
    reasons: list[str] = []
    if not bars:
        reasons.append("empty_series")
        return DataQualityResult(False, tuple(reasons))
    timestamps = [bar.timestamp for bar in bars]
    if timestamps != sorted(timestamps):
        reasons.append("out_of_order")
    if len(set(timestamps)) != len(timestamps):
        reasons.append("duplicate_timestamp")
    if any(bar.volume < 0 for bar in bars):
        reasons.append("negative_volume")
    if any(bar.close <= 0 for bar in bars):
        reasons.append("non_positive_price")
    return DataQualityResult(not reasons, tuple(reasons))
