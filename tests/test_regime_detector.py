from datetime import datetime, timedelta, timezone
from decimal import Decimal

from quant_ai.domain.models import AssetClass, Instrument, Market
from quant_ai.marketdata.models import Candle
from quant_ai.regime.detector import Regime, RegimeDetector


def make_bars(prices: list[int]) -> tuple[Candle, ...]:
    inst = Instrument("TEST", Market.USA, AssetClass.EQUITY, "USD", "NASDAQ")
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return tuple(Candle(inst, base + timedelta(days=i), Decimal(p), Decimal(p + 1), Decimal(p - 1), Decimal(p), Decimal(1000)) for i, p in enumerate(prices))


def test_detects_uptrend() -> None:
    prices = [100 + i for i in range(20)]
    assert RegimeDetector().detect(make_bars(prices), 20).regime == Regime.TREND_UP
