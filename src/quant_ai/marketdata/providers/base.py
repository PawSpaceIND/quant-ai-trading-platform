from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from quant_ai.domain.models import Instrument
from quant_ai.marketdata.models import Candle


class HistoricalMarketDataProvider(ABC):
    @abstractmethod
    def candles(
        self,
        instrument: Instrument,
        start: datetime,
        end: datetime,
        timeframe: str,
    ) -> tuple[Candle, ...]:
        raise NotImplementedError


class LiveMarketDataProvider(ABC):
    @abstractmethod
    def latest(self, instrument: Instrument) -> Candle:
        raise NotImplementedError
