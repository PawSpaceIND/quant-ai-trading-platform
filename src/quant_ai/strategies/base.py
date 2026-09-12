from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal

from quant_ai.domain.models import Side
from quant_ai.marketdata.models import Candle


@dataclass(frozen=True)
class StrategySignal:
    side: Side
    confidence: Decimal
    reason: str


class Strategy(ABC):
    strategy_id: str

    @abstractmethod
    def on_bar(self, history: tuple[Candle, ...]) -> StrategySignal | None:
        raise NotImplementedError
