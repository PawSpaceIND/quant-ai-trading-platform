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
