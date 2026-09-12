from datetime import datetime, timedelta, timezone
from decimal import Decimal

from quant_ai.backtest.costs import CostModel
from quant_ai.backtest.replay import ReplayEngine
from quant_ai.domain.models import AssetClass, Instrument, Market
from quant_ai.marketdata.models import Candle
from quant_ai.strategies.momentum import MomentumStrategy


def test_replay_is_deterministic_and_point_in_time() -> None:
    inst = Instrument("TEST", Market.USA, AssetClass.EQUITY, "USD", "TESTEX")
    now = datetime.now(timezone.utc)
    prices = [100, 101, 103, 104, 102]
    bars = tuple(Candle(inst, now + timedelta(days=i), Decimal(p), Decimal(p), Decimal(p), Decimal(p), Decimal(1)) for i, p in enumerate(prices))
    engine = ReplayEngine(CostModel(Decimal(0), Decimal(0), Decimal(0)))
    strategy = MomentumStrategy(2, Decimal("0.01"))
    first = engine.run(bars, strategy, Decimal(1000))
    second = engine.run(bars, strategy, Decimal(1000))
    assert first == second
    assert len(first.equity_curve) == len(bars) + 1
