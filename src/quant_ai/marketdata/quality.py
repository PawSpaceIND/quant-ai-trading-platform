from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from quant_ai.marketdata.models import Candle


@dataclass(frozen=True)
class DataQualityResult:
    valid: bool
    reasons: tuple[str, ...]


def validate_series(bars: tuple[Candle, ...], *, max_age_seconds: int | None = None) -> DataQualityResult:
    reasons: list[str] = []
    if not bars:
        return DataQualityResult(False, ("no_bars",))
    timestamps = [bar.timestamp for bar in bars]
    if timestamps != sorted(timestamps):
        reasons.append("non_monotonic_timestamps")
    if len(set(timestamps)) != len(timestamps):
        reasons.append("duplicate_timestamps")
    if any(bar.volume < 0 for bar in bars):
        reasons.append("negative_volume")
    identities = {
        (bar.instrument.symbol, bar.instrument.market, bar.instrument.asset_class, bar.instrument.currency, bar.instrument.exchange)
        for bar in bars
    }
    if len(identities) != 1:
        reasons.append("mixed_instruments")
    if max_age_seconds is not None:
        age = (datetime.now(timezone.utc) - bars[-1].timestamp).total_seconds()
        if age > max_age_seconds:
            reasons.append("stale_data")
    return DataQualityResult(not reasons, tuple(reasons))


def validate_bars(bars: tuple[Candle, ...], *, max_age_seconds: int | None = None) -> DataQualityResult:
    return validate_series(bars, max_age_seconds=max_age_seconds)
