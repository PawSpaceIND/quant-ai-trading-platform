from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant_ai.backtesting.replay import (
    HistoricalMarketDataFeed,
    HistoricalNewsProvider,
    HistoricalReplayDataset,
    HistoricalReplayHarness,
)
from quant_ai.domain.models import AssetClass, Instrument, Market, RiskMode
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.intelligence.providers import NewsSignal
from quant_ai.marketdata.models import Candle
from quant_ai.planning.capital import CapitalGoalEngine, CapitalPlanRequest

AAPL = Instrument("AAPL", Market.USA, AssetClass.EQUITY, "USD", "NASDAQ")
T0 = datetime(2026, 9, 14, 14, tzinfo=timezone.utc)


def _dataset(count: int = 6) -> HistoricalReplayDataset:
    bars = tuple(
        Candle(AAPL, T0 + timedelta(minutes=i), Decimal(200), Decimal(201), Decimal(199), Decimal(200), Decimal(1000))
        for i in range(count)
    )
    return HistoricalReplayDataset(bars)


def _plan():
    return CapitalGoalEngine().recommend(CapitalPlanRequest(
        Decimal(100000), Decimal("0.8"), Decimal("0.2"), expected_edge=Decimal("0.02"), requested_mode=RiskMode.BALANCED,
    ))


def test_replay_guard_catches_a_feed_that_serves_future_bars(tmp_path, monkeypatch) -> None:
    # Sabotage the feed so the pipeline would be handed bars from after the simulation clock.
    monkeypatch.setattr(
        HistoricalMarketDataFeed, "fetch_ohlcv",
        lambda self, instrument, start, end, timeframe="1m": self.bars,
    )
    broker = PaperBrokerService(tmp_path / "leak.db", starting_capital=Decimal(100000), slippage_bps=Decimal(0))
    with pytest.raises(RuntimeError, match="lookahead_violation: future bar"):
        HistoricalReplayHarness(broker, _plan(), quantity=1).run(_dataset())


def test_replay_guard_catches_a_provider_that_leaks_future_events(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(HistoricalNewsProvider, "fetch", lambda self, subject, now: self.events)
    dataset = HistoricalReplayDataset(
        _dataset().bars,
        news=(NewsSignal("AAPL", "tomorrow's headline", Decimal("0.5"), "t", T0 + timedelta(minutes=5)),),
    )
    broker = PaperBrokerService(tmp_path / "leak2.db", starting_capital=Decimal(100000), slippage_bps=Decimal(0))
    with pytest.raises(RuntimeError, match="lookahead_violation: future provider event"):
        HistoricalReplayHarness(broker, _plan(), quantity=1).run(dataset)


def test_honest_replay_passes_the_guard(tmp_path) -> None:
    broker = PaperBrokerService(tmp_path / "clean.db", starting_capital=Decimal(100000), slippage_bps=Decimal(0))
    result = HistoricalReplayHarness(broker, _plan(), quantity=1).run(_dataset())
    assert len(result.timestamps) == 6
