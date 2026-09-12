from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from quant_ai.domain.models import Instrument, Opportunity, Side, Signal


@dataclass(frozen=True)
class EnsembleConfig:
    min_confidence: Decimal = Decimal("0.60")
    min_probability: Decimal = Decimal("0.55")
    min_expected_value: Decimal = Decimal("0.001")
    reward_risk_ratio: Decimal = Decimal("2.0")


class SignalEnsemble:
    def __init__(self, config: EnsembleConfig | None = None) -> None:
        self.config = config or EnsembleConfig()

    def score(self, instrument: Instrument, signals: tuple[Signal, ...]) -> Opportunity | None:
        if not signals:
            return None
        total_weight = sum((s.confidence for s in signals), Decimal(0))
        if total_weight <= 0:
            return None
        weighted_score = sum((s.score * s.confidence for s in signals), Decimal(0)) / total_weight
        confidence = min(Decimal(1), total_weight / Decimal(len(signals)))
        probability = (weighted_score + Decimal(1)) / Decimal(2)
        side = Side.BUY if weighted_score >= 0 else Side.SELL
        probability = probability if side is Side.BUY else Decimal(1) - probability
        expected_return = max(Decimal(0), probability * Decimal("0.03"))
        expected_loss = (Decimal(1) - probability) * Decimal("0.015")
        expected_value = expected_return - expected_loss
        stop_distance = Decimal("0.01")
        take_profit_distance = stop_distance * self.config.reward_risk_ratio
        if confidence < self.config.min_confidence:
            return None
        if probability < self.config.min_probability:
            return None
        if expected_value < self.config.min_expected_value:
            return None
        return Opportunity(
            instrument=instrument,
            side=side,
            probability=probability,
            expected_return=expected_return,
            expected_loss=expected_loss,
            expected_value=expected_value,
            confidence=confidence,
            stop_distance=stop_distance,
            take_profit_distance=take_profit_distance,
            signals=signals,
        )
