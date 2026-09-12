from datetime import datetime, timedelta, timezone
from decimal import Decimal

from quant_ai.domain.models import AssetClass, Instrument, Market
from quant_ai.features.technical import average_true_range, rolling_volatility, simple_return
from quant_ai.marketdata.models import Candle


def bars(prices: list[int]) -> tuple[Candle, ...]:
    instrument = Instrument("TEST", Market.USA, AssetClass.EQUITY, "USD", "NASDAQ")
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return tuple(Candle(instrument, base + timedelta(days=i), Decimal(p), Decimal(p + 2), Decimal(p - 2), Decimal(p), Decimal(1000)) for i, p in enumerate(prices))


def test_features_are_deterministic() -> None:
    history = bars([100, 101, 103, 102, 105])
    assert simple_return(history, 2) == Decimal(2) / Decimal(103)
    assert average_true_range(history, 3) > 0
    assert rolling_volatility(history, 5) > 0
