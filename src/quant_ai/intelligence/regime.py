from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from enum import Enum

from quant_ai.domain.models import RiskMode
from quant_ai.marketdata.models import Candle
from quant_ai.planning.capital import CapitalPlan


class MarketRegime(str, Enum):
    BULL_TRENDING = "BULL_TRENDING"
    BEAR_TRENDING = "BEAR_TRENDING"
    MEAN_REVERTING = "MEAN_REVERTING"
    HIGH_VOL_CRISIS = "HIGH_VOL_CRISIS"


@dataclass(frozen=True)
class RegimeAssessment:
    regime: MarketRegime
    atr_fraction: Decimal
    adx: Decimal
    trend_return: Decimal
    gross_exposure_multiplier: Decimal


class MarketRegimeDetector:
    def __init__(
        self,
        *,
        crisis_atr_fraction: Decimal = Decimal("0.04"),
        trend_adx_threshold: Decimal = Decimal(25),
        adx_period: int = 14,
    ) -> None:
        if crisis_atr_fraction <= 0 or trend_adx_threshold <= 0:
            raise ValueError("regime thresholds must be positive")
        if adx_period < 2:
            raise ValueError("adx_period must be at least two")
        self.crisis_atr_fraction = crisis_atr_fraction
        self.trend_adx_threshold = trend_adx_threshold
        self.adx_period = adx_period

    def detect(self, candles: tuple[Candle, ...]) -> RegimeAssessment:
        if len(candles) < 15:
            return RegimeAssessment(
                MarketRegime.MEAN_REVERTING,
                Decimal(0),
                Decimal(0),
                Decimal(0),
                Decimal("0.80"),
            )
        atr = self._atr(candles[-15:])
        last = candles[-1].close
        atr_fraction = atr / last if last > 0 else Decimal(0)
        adx = self._adx(candles, self.adx_period)
        trend_return = (candles[-1].close - candles[-15].close) / candles[-15].close
        if atr_fraction >= self.crisis_atr_fraction:
            regime, multiplier = MarketRegime.HIGH_VOL_CRISIS, Decimal("0.35")
        elif adx >= self.trend_adx_threshold and trend_return > 0:
            regime, multiplier = MarketRegime.BULL_TRENDING, Decimal("1.00")
        elif adx >= self.trend_adx_threshold and trend_return < 0:
            regime, multiplier = MarketRegime.BEAR_TRENDING, Decimal("0.70")
        else:
            regime, multiplier = MarketRegime.MEAN_REVERTING, Decimal("0.80")
        return RegimeAssessment(regime, atr_fraction, adx, trend_return, multiplier)

    @staticmethod
    def _atr(candles: tuple[Candle, ...]) -> Decimal:
        ranges: list[Decimal] = []
        for before, current in zip(candles, candles[1:]):
            ranges.append(
                max(
                    current.high - current.low,
                    abs(current.high - before.close),
                    abs(current.low - before.close),
                )
            )
        return sum(ranges, Decimal(0)) / Decimal(len(ranges)) if ranges else Decimal(0)

    @staticmethod
    def _adx(candles: tuple[Candle, ...], period: int = 14) -> Decimal:
        """Wilder ADX using smoothed TR/+DM/-DM and a smoothed DX series."""
        if len(candles) < period * 2:
            return Decimal(0)
        true_ranges: list[Decimal] = []
        plus_dm: list[Decimal] = []
        minus_dm: list[Decimal] = []
        for before, current in zip(candles, candles[1:]):
            true_ranges.append(
                max(
                    current.high - current.low,
                    abs(current.high - before.close),
                    abs(current.low - before.close),
                )
            )
            up = current.high - before.high
            down = before.low - current.low
            plus_dm.append(up if up > down and up > 0 else Decimal(0))
            minus_dm.append(down if down > up and down > 0 else Decimal(0))

        smooth_tr = sum(true_ranges[:period], Decimal(0))
        smooth_plus = sum(plus_dm[:period], Decimal(0))
        smooth_minus = sum(minus_dm[:period], Decimal(0))
        dx_values: list[Decimal] = []

        def dx_value() -> Decimal:
            if smooth_tr <= 0:
                return Decimal(0)
            plus_di = Decimal(100) * smooth_plus / smooth_tr
            minus_di = Decimal(100) * smooth_minus / smooth_tr
            total = plus_di + minus_di
            return Decimal(100) * abs(plus_di - minus_di) / total if total > 0 else Decimal(0)

        dx_values.append(dx_value())
        divisor = Decimal(period)
        for index in range(period, len(true_ranges)):
            smooth_tr = smooth_tr - smooth_tr / divisor + true_ranges[index]
            smooth_plus = smooth_plus - smooth_plus / divisor + plus_dm[index]
            smooth_minus = smooth_minus - smooth_minus / divisor + minus_dm[index]
            dx_values.append(dx_value())

        if len(dx_values) < period:
            return Decimal(0)
        adx = sum(dx_values[:period], Decimal(0)) / divisor
        for value in dx_values[period:]:
            adx = (adx * Decimal(period - 1) + value) / divisor
        return adx

    @staticmethod
    def apply_to_plan(plan: CapitalPlan, assessment: RegimeAssessment) -> CapitalPlan:
        cap = plan.max_gross_exposure_fraction * assessment.gross_exposure_multiplier
        stop = plan.stop_loss_fraction
        if assessment.atr_fraction > 0:
            multiplier = {
                RiskMode.CONSERVATIVE: Decimal("1.25"),
                RiskMode.BALANCED: Decimal("1.50"),
                RiskMode.AGGRESSIVE: Decimal("1.75"),
            }[plan.recommended_mode]
            dailyized_atr = assessment.atr_fraction * Decimal(390).sqrt()
            stop = min(
                Decimal("0.05"),
                max(Decimal("0.006"), dailyized_atr * multiplier),
            )
        take_profit = stop * plan.reward_risk_ratio
        return replace(
            plan,
            max_gross_exposure_fraction=cap,
            stop_loss_fraction=stop,
            take_profit_fraction=take_profit,
            rationale=plan.rationale
            + (
                f"market_regime={assessment.regime.value}",
                f"regime_gross_multiplier={assessment.gross_exposure_multiplier}",
                f"live_stop_fraction={stop}",
            ),
        )


# ---------------------------------------------------------------- regime summaries
#
# The classifier below is evidence, not a sizing input. It labels the market the swarm is
# trading inside so the specialists, the consensus prompt and every proof carry the same
# deterministic regime; ``MarketRegimeDetector`` above keeps owning the plan multipliers.

TRENDING_UP = "trending_up"
TRENDING_DOWN = "trending_down"
RANGING = "ranging"
HIGH_VOLATILITY = "high_volatility"
INSUFFICIENT_HISTORY = "insufficient_history"
REGIME_LABELS = frozenset(
    {TRENDING_UP, TRENDING_DOWN, RANGING, HIGH_VOLATILITY, INSUFFICIENT_HISTORY}
)

# Thresholds. Every metric is quantized to REGIME_PRECISION before it is compared, so the
# label always agrees with the numbers rendered beside it.
#
# - REGIME_LOOKBACK: bars scored; about two months of sessions, or 1.5 sessions of
#   15-minute bars.
# - MIN_REGIME_BARS: ATR(14) needs 15 bars and the median needs several ATR values; below
#   20 the answer is ``insufficient_history``, never a guess.
# - TREND_STRENGTH_THRESHOLD: the fitted close drift must be at least 0.15 median-ATR per
#   bar, six ATRs across the lookback. A driftless random walk fits about 0.1 ATR per bar
#   one sigma of the time, so only clear directional drift qualifies.
# - VOLATILITY_RATIO_THRESHOLD: ATR(14) now must be half again the lookback's median
#   ATR(14). The rolling mean is quiet, so 1.5x marks a real expansion, not rotation.
# - RANGE_BAND_ATR_MULTIPLE: the share of closes within one median ATR of the lookback
#   mean; descriptive evidence only, the priority order below never reads it.
REGIME_LOOKBACK = 40
MIN_REGIME_BARS = 20
REGIME_ATR_PERIOD = 14
TREND_STRENGTH_THRESHOLD = Decimal("0.15")
VOLATILITY_RATIO_THRESHOLD = Decimal("1.5")
RANGE_BAND_ATR_MULTIPLE = Decimal(1)
REGIME_PRECISION = Decimal("0.0001")


@dataclass(frozen=True)
class RegimeSummary:
    """One deterministic regime label with the metrics that produced it.

    ``trend_strength`` is the regression slope of closes per bar in units of the
    lookback's median ATR(14), signed. ``volatility_ratio`` is the current ATR(14) over
    that median. ``range_fraction`` is the share of closes inside one median ATR of the
    lookback mean. ``bars_used`` is the bars scored, or the bars that were available
    when history was insufficient.
    """

    label: str
    timeframe: str
    bars_used: int
    trend_strength: Decimal
    volatility_ratio: Decimal
    range_fraction: Decimal

    def __post_init__(self) -> None:
        if self.label not in REGIME_LABELS:
            raise ValueError(f"unknown regime label: {self.label}")
        if not self.timeframe or any(char.isspace() or char in ";=" for char in self.timeframe):
            raise ValueError("timeframe must be a single token")
        if self.bars_used < 0:
            raise ValueError("bars_used cannot be negative")
        if self.volatility_ratio < 0 or not Decimal(0) <= self.range_fraction <= Decimal(1):
            raise ValueError("volatility_ratio must be non-negative and range_fraction in [0, 1]")

    @classmethod
    def insufficient(cls, timeframe: str, bars_available: int = 0) -> RegimeSummary:
        return cls(INSUFFICIENT_HISTORY, timeframe, bars_available, Decimal(0), Decimal(0), Decimal(0))

    @property
    def classified(self) -> bool:
        return self.label != INSUFFICIENT_HISTORY

    def as_evidence(self) -> tuple[tuple[str, Decimal], ...]:
        """The numeric metrics as ordered ``(name, value)`` pairs for evidence maps."""
        return (
            ("bars_used", Decimal(self.bars_used)),
            ("trend_strength", self.trend_strength),
            ("volatility_ratio", self.volatility_ratio),
            ("range_fraction", self.range_fraction),
        )

    def describe(self) -> str:
        """``trending_up (1d, 40 bars)`` for rationales, logs and dashboards."""
        return f"{self.label} ({self.timeframe}, {self.bars_used} bars)"

    def render(self) -> str:
        """One ``key=value;...`` data line: label, timeframe, then the metrics."""
        metrics = ";".join(f"{name}={value}" for name, value in self.as_evidence())
        return f"label={self.label};timeframe={self.timeframe};{metrics}"


def _quantized(value: Decimal) -> Decimal:
    return value.quantize(REGIME_PRECISION)


def _median(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal(2)


def _regression_slope(values: tuple[Decimal, ...]) -> Decimal:
    """Ordinary least-squares slope of ``values`` against their 0-based index."""
    count = len(values)
    if count < 2:
        return Decimal(0)
    mean_x = Decimal(count - 1) / Decimal(2)
    mean_y = sum(values, Decimal(0)) / Decimal(count)
    covariance = sum(
        ((Decimal(index) - mean_x) * (value - mean_y) for index, value in enumerate(values)),
        Decimal(0),
    )
    variance = sum(((Decimal(index) - mean_x) ** 2 for index in range(count)), Decimal(0))
    return covariance / variance if variance > 0 else Decimal(0)


def _true_ranges(bars: tuple[Candle, ...]) -> list[Decimal]:
    return [
        max(
            current.high - current.low,
            abs(current.high - before.close),
            abs(current.low - before.close),
        )
        for before, current in zip(bars, bars[1:])
    ]


def classify(
    bars: tuple[Candle, ...],
    *,
    timeframe: str,
    lookback: int = REGIME_LOOKBACK,
    min_bars: int = MIN_REGIME_BARS,
    atr_period: int = REGIME_ATR_PERIOD,
    trend_threshold: Decimal = TREND_STRENGTH_THRESHOLD,
    volatility_threshold: Decimal = VOLATILITY_RATIO_THRESHOLD,
    band_atr_multiple: Decimal = RANGE_BAND_ATR_MULTIPLE,
) -> RegimeSummary:
    """Label the newest ``lookback`` closed bars; a pure function of its inputs.

    Priority: ``high_volatility`` when ``volatility_ratio`` exceeds its threshold; else
    ``trending_up`` / ``trending_down`` when ``|trend_strength|`` exceeds its threshold;
    else ``ranging``. Fewer than ``min_bars`` bars is ``insufficient_history``. A
    dead-flat lookback (zero median ATR) has no unit to normalise by: it reads as a
    ratio of one and a slope of zero, that is ``ranging``, never a spike.
    """
    if atr_period < 2:
        raise ValueError("atr_period must be at least two")
    if min_bars < atr_period + 2:
        raise ValueError("min_bars must leave at least two ATR values for the median")
    if lookback < min_bars:
        raise ValueError("lookback cannot be below min_bars")
    if trend_threshold <= 0 or volatility_threshold <= 0 or band_atr_multiple <= 0:
        raise ValueError("thresholds must be positive")
    window = bars[-lookback:]
    count = len(window)
    if count < min_bars:
        return RegimeSummary.insufficient(timeframe, len(bars))

    closes = tuple(bar.close for bar in window)
    ranges = _true_ranges(window)
    atr_series = [
        sum(ranges[end - atr_period : end], Decimal(0)) / Decimal(atr_period)
        for end in range(atr_period, len(ranges) + 1)
    ]
    current_atr = atr_series[-1]
    median_atr = _median(atr_series)
    unit = median_atr if median_atr > 0 else current_atr
    volatility_ratio = current_atr / median_atr if median_atr > 0 else Decimal(1)
    trend_strength = _regression_slope(closes) / unit if unit > 0 else Decimal(0)
    mean_close = sum(closes, Decimal(0)) / Decimal(count)
    band = band_atr_multiple * unit
    inside = sum(1 for close in closes if abs(close - mean_close) <= band)
    range_fraction = Decimal(inside) / Decimal(count)

    volatility_ratio = _quantized(volatility_ratio)
    trend_strength = _quantized(trend_strength)
    range_fraction = _quantized(range_fraction)
    if volatility_ratio > volatility_threshold:
        label = HIGH_VOLATILITY
    elif trend_strength > trend_threshold:
        label = TRENDING_UP
    elif trend_strength < -trend_threshold:
        label = TRENDING_DOWN
    else:
        label = RANGING
    return RegimeSummary(label, timeframe, count, trend_strength, volatility_ratio, range_fraction)


def primary_regime(*summaries: RegimeSummary) -> RegimeSummary:
    """The first classified summary in priority order (daily before intraday).

    When none is classified the ``insufficient_history`` summary with the most bars is
    returned, earlier ones winning ties, so the proof still says what was looked at.
    """
    if not summaries:
        raise ValueError("at least one summary is required")
    for summary in summaries:
        if summary.classified:
            return summary
    return max(summaries, key=lambda summary: summary.bars_used)
