from datetime import datetime, timezone
from decimal import Decimal

from quant_ai.domain.models import AssetClass, Instrument, Market
from quant_ai.marketdata.actions import CorporateAction, adjust_candle
from quant_ai.marketdata.aggregate import aggregate_bars
from quant_ai.marketdata.models import Candle
from quant_ai.marketdata.quality import validate_series
from quant_ai.marketdata.session import SESSIONS


def instrument() -> Instrument:
    return Instrument("AAPL", Market.USA, AssetClass.EQUITY, "USD", "NASDAQ")


def test_market_session_and_aggregation() -> None:
    assert SESSIONS[Market.USA].is_open(datetime(2026, 9, 14, 14, 0, tzinfo=timezone.utc))
    bars = (
        Candle(instrument(), datetime(2026, 1, 1, 14, 30, tzinfo=timezone.utc), Decimal(100), Decimal(102), Decimal(99), Decimal(101), Decimal(10)),
        Candle(instrument(), datetime(2026, 1, 1, 14, 31, tzinfo=timezone.utc), Decimal(101), Decimal(104), Decimal(100), Decimal(103), Decimal(20)),
    )
    agg = aggregate_bars(bars)
    assert agg.open == Decimal(100)
    assert agg.close == Decimal(103)
    assert agg.volume == Decimal(30)
    assert validate_series(bars).valid


def test_corporate_action_adjustment() -> None:
    bar = Candle(instrument(), datetime(2026, 1, 1, tzinfo=timezone.utc), Decimal(200), Decimal(210), Decimal(190), Decimal(205), Decimal(100))
    adjusted = adjust_candle(bar, CorporateAction(Decimal(2), Decimal(0)))
    assert adjusted.close == Decimal("102.5")
    assert adjusted.volume == Decimal(200)
