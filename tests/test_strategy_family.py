from datetime import datetime, timedelta, timezone
from decimal import Decimal

from quant_ai.domain.models import AssetClass, Instrument, Market, Side
from quant_ai.marketdata.models import Candle
from quant_ai.strategies.breakout import BreakoutStrategy
from quant_ai.strategies.mean_reversion import MeanReversionStrategy


def bars(prices: list[int]) -> tuple[Candle, ...]:
    inst = Instrument("TEST", Market.USA, AssetClass.EQUITY, "USD", "NASDAQ")
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return tuple(Candle(inst, base + timedelta(days=i), Decimal(p), Decimal(p + 1), Decimal(p - 1), Decimal(p), Decimal(1000)) for i, p in enumerate(prices))


def test_breakout_buy() -> None:
    signal = BreakoutStrategy(lookback=3).on_bar(bars([100, 101, 102, 105]))
    assert signal and signal.side == Side.BUY


def test_mean_reversion_sell() -> None:
    signal = MeanReversionStrategy(lookback=4, deviation=Decimal("0.02")).on_bar(bars([100, 100, 100, 106]))
    assert signal and signal.side == Side.SELL
