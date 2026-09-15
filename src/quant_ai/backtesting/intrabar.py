"""Chronological lower-timeframe protection with explicit OHLC ambiguity.

Candle timestamps are interval CLOSE times. Windows declare their opening time
and fixed lower-bar interval. Missing bars or parent/child price mismatches fail
closed; a parent OHLC path is never invented as replacement data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from quant_ai.marketdata.models import Candle


@dataclass(frozen=True)
class IntrabarWindow:
    parent_timestamp: datetime
    start: datetime
    interval_seconds: int
    bars: tuple[Candle, ...]

    def validate(self, parent: Candle, previous_close: datetime) -> None:
        if self.start.tzinfo is None or self.parent_timestamp.tzinfo is None:
            raise ValueError("intrabar timestamps must be timezone-aware")
        if self.parent_timestamp != parent.timestamp or self.start < previous_close:
            raise ValueError("intrabar window overlaps prior decision or mismatches parent")
        if type(self.interval_seconds) is not int or self.interval_seconds <= 0 or not self.bars:
            raise ValueError("intrabar coverage is required")
        step = timedelta(seconds=self.interval_seconds)
        for index, bar in enumerate(self.bars, 1):
            if bar.instrument != parent.instrument or bar.timestamp != self.start + index * step:
                raise ValueError("intrabar coverage must be contiguous and instrument-matched")
        if self.bars[-1].timestamp != parent.timestamp:
            raise ValueError("intrabar coverage must reach the parent close")
        if (
            self.bars[0].open != parent.open
            or self.bars[-1].close != parent.close
            or max(b.high for b in self.bars) != parent.high
            or min(b.low for b in self.bars) != parent.low
        ):
            raise ValueError("intrabar OHLC does not reconcile to parent")


@dataclass(frozen=True)
class IntrabarBreach:
    trigger: str
    reference_price: Decimal
    interval_start: datetime
    interval_end: datetime
    gap_at_open: bool
    ambiguous: bool


def first_breach(
    window: IntrabarWindow, stop: Decimal | None, target: Decimal | None
) -> IntrabarBreach | None:
    """Long positions only; ambiguous lower-bar touches resolve stop first.

    Open gaps use the observed open, not a guaranteed stop price. Range touches
    use their threshold before the broker friction model. Time is an interval,
    not a claimed exact tick. Targets are market-style triggers, not limit orders.
    """
    if any(level is not None and (not level.is_finite() or level <= 0) for level in (stop, target)):
        raise ValueError("protective thresholds must be finite and positive")
    if stop is not None and target is not None and stop >= target:
        raise ValueError("stop must be below target")
    for bar in window.bars:
        start = bar.timestamp - timedelta(seconds=window.interval_seconds)
        if stop is not None and bar.open <= stop:
            return IntrabarBreach("STOP_LOSS", bar.open, start, bar.timestamp, True, False)
        if target is not None and bar.open >= target:
            return IntrabarBreach("TAKE_PROFIT", bar.open, start, bar.timestamp, True, False)
        stop_hit = stop is not None and bar.low <= stop
        target_hit = target is not None and bar.high >= target
        if stop_hit:
            return IntrabarBreach("STOP_LOSS", stop, start, bar.timestamp, False, target_hit)
        if target_hit:
            return IntrabarBreach("TAKE_PROFIT", target, start, bar.timestamp, False, False)
    return None
