from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from enum import Enum

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
    ) -> None:
        if crisis_atr_fraction <= 0 or trend_adx_threshold <= 0:
            raise ValueError("regime thresholds must be positive")
        self.crisis_atr_fraction = crisis_atr_fraction
        self.trend_adx_threshold = trend_adx_threshold

    def detect(self, candles: tuple[Candle, ...]) -> RegimeAssessment:
        if len(candles) < 15:
            return RegimeAssessment(MarketRegime.MEAN_REVERTING, Decimal(0), Decimal(0), Decimal(0), Decimal("0.80"))
        atr = self._atr(candles[-15:])
        last = candles[-1].close
        atr_fraction = atr / last if last > 0 else Decimal(0)
        adx = self._adx(candles[-15:])
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
        ranges = []
        for before, current in zip(candles, candles[1:]):
            ranges.append(max(
                current.high - current.low,
                abs(current.high - before.close),
                abs(current.low - before.close),
            ))
        return sum(ranges, Decimal(0)) / Decimal(len(ranges)) if ranges else Decimal(0)

    @staticmethod
    def _adx(candles: tuple[Candle, ...]) -> Decimal:
        tr_sum = Decimal(0)
        plus_sum = Decimal(0)
        minus_sum = Decimal(0)
        for before, current in zip(candles, candles[1:]):
            tr = max(
                current.high - current.low,
                abs(current.high - before.close),
                abs(current.low - before.close),
            )
            up = current.high - before.high
            down = before.low - current.low
            plus_dm = up if up > down and up > 0 else Decimal(0)
            minus_dm = down if down > up and down > 0 else Decimal(0)
            tr_sum += tr
            plus_sum += plus_dm
            minus_sum += minus_dm
        if tr_sum <= 0:
            return Decimal(0)
        plus_di = Decimal(100) * plus_sum / tr_sum
        minus_di = Decimal(100) * minus_sum / tr_sum
        total = plus_di + minus_di
        return Decimal(100) * abs(plus_di - minus_di) / total if total > 0 else Decimal(0)

    @staticmethod
    def apply_to_plan(plan: CapitalPlan, assessment: RegimeAssessment) -> CapitalPlan:
        cap = plan.max_gross_exposure_fraction * assessment.gross_exposure_multiplier
        return replace(
            plan,
            max_gross_exposure_fraction=cap,
            rationale=plan.rationale + (
                f"market_regime={assessment.regime.value}",
                f"regime_gross_multiplier={assessment.gross_exposure_multiplier}",
            ),
        )
