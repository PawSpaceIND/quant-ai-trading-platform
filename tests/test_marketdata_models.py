from datetime import datetime, timezone
from decimal import Decimal

import pytest

from quant_ai.domain.models import AssetClass, Instrument, Market
from quant_ai.marketdata.models import Candle


def instrument() -> Instrument:
    return Instrument("AAPL", Market.USA, AssetClass.EQUITY, "USD", "NASDAQ")


def test_valid_candle() -> None:
    bar = Candle(instrument(), datetime.now(timezone.utc), Decimal(100), Decimal(105), Decimal(99), Decimal(103), Decimal(1000))
    assert bar.close == Decimal(103)


def test_rejects_invalid_range() -> None:
    with pytest.raises(ValueError):
        Candle(instrument(), datetime.now(timezone.utc), Decimal(100), Decimal(99), Decimal(101), Decimal(100), Decimal(1))
