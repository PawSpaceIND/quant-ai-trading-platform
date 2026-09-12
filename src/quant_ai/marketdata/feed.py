from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from quant_ai.domain.models import Instrument, Market
from quant_ai.marketdata.models import Candle


@dataclass(frozen=True)
class MarketTick:
    instrument: Instrument
    timestamp: datetime
    last_price: Decimal
    bid: Decimal
    ask: Decimal
    volume: Decimal

    def __post_init__(self) -> None:
        if min(self.last_price, self.bid, self.ask) <= 0:
            raise ValueError("tick prices must be positive")
        if self.bid > self.ask:
            raise ValueError("bid cannot exceed ask")
        if self.volume < 0:
            raise ValueError("volume cannot be negative")


class MarketDataFeed(ABC):
    @abstractmethod
    def fetch_ohlcv(
        self,
        instrument: Instrument,
        start: datetime,
        end: datetime,
        timeframe: str = "1m",
    ) -> tuple[Candle, ...]:
        raise NotImplementedError

    @abstractmethod
    def latest_tick(self, instrument: Instrument) -> MarketTick:
        raise NotImplementedError


class SandboxMarketDataFeed(MarketDataFeed):
    """Deterministic local feed; never performs external HTTP requests."""

    market: Market
    exchange: str

    def __init__(self, market: Market, exchange: str, base_price: Decimal) -> None:
        self.market = market
        self.exchange = exchange
        self.base_price = base_price

    def _validate(self, instrument: Instrument) -> None:
        if instrument.market != self.market:
            raise ValueError("instrument_market_mismatch")
        if instrument.exchange.upper() != self.exchange.upper():
            raise ValueError("instrument_exchange_mismatch")

    def fetch_ohlcv(
        self,
        instrument: Instrument,
        start: datetime,
        end: datetime,
        timeframe: str = "1m",
    ) -> tuple[Candle, ...]:
        self._validate(instrument)
        if end <= start:
            raise ValueError("end must be after start")
        if timeframe not in {"1m", "5m", "15m", "1h", "1d"}:
            raise ValueError("unsupported_timeframe")
        step = {
            "1m": timedelta(minutes=1),
            "5m": timedelta(minutes=5),
            "15m": timedelta(minutes=15),
            "1h": timedelta(hours=1),
            "1d": timedelta(days=1),
        }[timeframe]
        candles: list[Candle] = []
        timestamp = start
        index = 0
        while timestamp < end and index < 5000:
            drift = Decimal(index % 7) / Decimal(100)
            open_price = self.base_price + drift
            close = open_price + Decimal("0.05")
            candles.append(
                Candle(
                    instrument,
                    timestamp,
                    open_price,
                    close + Decimal("0.05"),
                    open_price - Decimal("0.05"),
                    close,
                    Decimal(1000 + index * 10),
                )
            )
            timestamp += step
            index += 1
        return tuple(candles)

    def latest_tick(self, instrument: Instrument) -> MarketTick:
        self._validate(instrument)
        return MarketTick(
            instrument,
            datetime.now(timezone.utc),
            self.base_price,
            self.base_price - Decimal("0.01"),
            self.base_price + Decimal("0.01"),
            Decimal(1000),
        )


class IndiaSandboxMarketDataFeed(SandboxMarketDataFeed):
    def __init__(self, base_price: Decimal = Decimal(2500)) -> None:
        super().__init__(Market.INDIA, "NSE", base_price)


class UsaSandboxMarketDataFeed(SandboxMarketDataFeed):
    def __init__(self, base_price: Decimal = Decimal(200), exchange: str = "NASDAQ") -> None:
        if exchange.upper() not in {"NASDAQ", "NYSE"}:
            raise ValueError("unsupported_us_exchange")
        super().__init__(Market.USA, exchange.upper(), base_price)
