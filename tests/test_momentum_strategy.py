from datetime import datetime, timedelta, timezone
from decimal import Decimal

from quant_ai.domain.models import AssetClass, Instrument, Market, Side
from quant_ai.marketdata.models import Candle
from quant_ai.strategies.momentum import MomentumStrategy


def bars(prices: list[int]) -> tuple[Candle, ...]:
    inst = Instrument("TEST", Market.USA, AssetClass.EQUITY, "USD", "TESTEX")
    now = datetime.now(timezone.utc)
    return tuple(Candle(inst, now + timedelta(days=i), Decimal(p), Decimal(p), Decimal(p), Decimal(p), Decimal(1)) for i, p in enumerate(prices))


def test_momentum_buy_signal() -> None:
    signal = MomentumStrategy(lookback=2, threshold=Decimal("0.01")).on_bar(bars([100, 101, 103]))
    assert signal is not None
    assert signal.side is Side.BUY
