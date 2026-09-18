"""Last analysed regime context; observation-only, bounded and provider-free.

Snapshots describe the context computed by the pipeline, not an accepted trade,
current quotes, historical-data readiness, or the portfolio risk detector.
"""
from __future__ import annotations

import json
from collections import OrderedDict
from datetime import datetime, timezone
from decimal import Decimal
from threading import Lock

from quant_ai.intelligence.regime import MIN_REGIME_BARS, REGIME_LOOKBACK, RegimeSummary

SCHEMA = "pramana.regime_observation.v1"
MAX_OBSERVATIONS = 64  # Display cache bound, not a trading/watchlist cap.


def _instant(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("regime_observation_requires_aware_time")
    return value.astimezone(timezone.utc)


def _key(instrument):
    return (instrument.symbol, instrument.market, instrument.asset_class,
            instrument.currency, instrument.exchange, instrument.tradable,
            instrument.expiry, instrument.lot_size, instrument.tick_size,
            instrument.underlying, tuple(sorted(instrument.metadata.items())))


def _count(value):
    if type(value) is not int or not 0 <= value <= 1000000:
        raise ValueError("regime_observation_count_invalid")
    return value


def _summary(summary, available, timeframe):
    if not isinstance(summary, RegimeSummary) or summary.timeframe != timeframe:
        raise ValueError("regime_observation_summary_invalid")
    available, used = _count(available), _count(summary.bars_used)
    if used != min(available, REGIME_LOOKBACK) or summary.classified != (used >= MIN_REGIME_BARS):
        raise ValueError("regime_observation_count_mismatch")
    result = {"timeframe": timeframe, "label": summary.label,
              "barsUsed": used, "barsAvailable": available, "classified": summary.classified}
    for name, value in (("trendStrength", summary.trend_strength),
                        ("volatilityRatio", summary.volatility_ratio),
                        ("rangeFraction", summary.range_fraction)):
        if not isinstance(value, Decimal) or not value.is_finite():
            raise ValueError("regime_observation_metric_invalid")
        result[name] = str(value)
    return result


class RegimeObservationStore:
    def __init__(self):
        self._lock = Lock()
        self._observations = OrderedDict()

    def record(self, instrument, now, technical_bars, context):
        """Capture actual summaries without retaining bars or changing decision inputs."""
        moment = _instant(now)
        key = _key(instrument)
        try:
            daily = _summary(context.daily, len(context.daily_bars), "1d")
            intraday = _summary(context.intraday, len(context.intraday_bars), "15m")
            primary = context.primary
            if context.daily.classified:
                selected, reason = context.daily, "daily_classified"
            elif context.intraday.classified:
                selected, reason = context.intraday, "intraday_fallback"
            else:
                selected = max((context.daily, context.intraday), key=lambda item: item.bars_used)
                reason = "neither_classified_most_bars_daily_tiebreak"
            if primary != selected:
                raise ValueError("regime_observation_selection_mismatch")
            payload = {"schema": SCHEMA, "state": "observed", "observedAt": moment.isoformat(),
                       "technicalTimeframe": "1m", "technicalBars": _count(technical_bars),
                       "minimumBars": MIN_REGIME_BARS, "lookbackBars": REGIME_LOOKBACK,
                       "selectedTimeframe": primary.timeframe, "selectedLabel": primary.label,
                       "selectionReason": reason, "daily": daily, "intraday": intraday}
        except (ValueError, TypeError, AttributeError, ArithmeticError):
            # An optional diagnostic must not interrupt trading/protection or leak details.
            payload = {"schema": SCHEMA, "state": "unavailable", "observedAt": moment.isoformat()}
        raw = json.dumps(payload, allow_nan=False)
        with self._lock:
            previous = self._observations.get(key)
            if previous is not None and previous[0] > moment:
                return  # An older concurrent analysis cannot replace a newer observation.
            self._observations[key] = (moment, raw)
            self._observations.move_to_end(key)
            while len(self._observations) > MAX_OBSERVATIONS:
                self._observations.popitem(last=False)

    def snapshot(self, instrument, now):
        """Never wait behind analysis or perform network/database/provider operations."""
        try:
            moment, key = _instant(now), _key(instrument)
        except (ValueError, TypeError, AttributeError):
            return {"schema": SCHEMA, "state": "unavailable"}
        if not self._lock.acquire(blocking=False):
            return {"schema": SCHEMA, "state": "busy"}
        try:
            saved = self._observations.get(key)
        finally:
            self._lock.release()
        if saved is None:
            return {"schema": SCHEMA, "state": "not_observed"}
        observed, raw = saved
        if observed > moment:
            return {"schema": SCHEMA, "state": "future_observation"}
        return {**json.loads(raw), "ageSeconds": (moment - observed).total_seconds()}
