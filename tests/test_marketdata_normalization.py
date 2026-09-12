from datetime import datetime, timezone
from decimal import Decimal

from quant_ai.domain.models import AssetClass, Instrument, Market
from quant_ai.marketdata.aggregate import aggregate_windows
from quant_ai.marketdata.models import Candle
from quant_ai.marketdata.quality import validate_series
from quant_ai.marketdata.session import SESSIONS


def inst() -> Instrument:
    return Instrument("TEST", Market.USA, AssetClass.EQUITY, "USD", "NASDAQ")


def bar(ts: datetime, price: int) -> Candle:
    p = Decimal(price)
    return Candle(inst(), ts, p, p + 1, p - 1, p, Decimal(10))


def test_sessions_are_timezone_aware() -> None:
    assert SESSIONS[Market.INDIA].is_open(datetime(2026, 9, 14, 4, 0, tzinfo=timezone.utc))
    assert SESSIONS[Market.USA].is_open(datetime(2026, 9, 14, 14, 0, tzinfo=timezone.utc))


def test_multi_timeframe_aggregation_and_quality() -> None:
    bars = (
        bar(datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc), 100),
        bar(datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc), 101),
    )
    assert validate_series(bars).valid
    aggregated = aggregate_windows(bars, 2)
    assert len(aggregated) == 1
    assert aggregated[0].open == Decimal(100)
    assert aggregated[0].close == Decimal(101)
