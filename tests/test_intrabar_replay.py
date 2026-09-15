from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from quant_ai.backtesting.intrabar import IntrabarWindow, first_breach
from quant_ai.backtesting.replay import HistoricalReplayDataset, HistoricalReplayHarness
from quant_ai.domain.models import AssetClass, Instrument, Market, OrderIntent, RiskMode, Side
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.intelligence.pipeline import SwarmMarketAnalysisPipeline
from quant_ai.marketdata.models import Candle
from quant_ai.planning.capital import CapitalGoalEngine, CapitalPlanRequest

INSTRUMENT = Instrument("AAPL", Market.USA, AssetClass.EQUITY, "USD", "NASDAQ")
START = datetime(2026, 9, 14, 14, 0, tzinfo=timezone.utc)


def candle(minute, opening=100, high=104, low=96, close=100):
    return Candle(
        INSTRUMENT,
        START + timedelta(minutes=minute),
        D(opening),
        D(high),
        D(low),
        D(close),
        D(1000),
    )


def window(*bars):
    return IntrabarWindow(bars[-1].timestamp, START, 60, bars)


def plan():
    return CapitalGoalEngine().recommend(
        CapitalPlanRequest(
            D(100000),
            D("0.8"),
            D("0.2"),
            expected_edge=D("0.02"),
            requested_mode=RiskMode.BALANCED,
        )
    )


def test_lower_bars_resolve_target_before_later_stop():
    result = first_breach(window(candle(1, high=111), candle(2, low=90)), D(95), D(110))
    assert result.trigger == "TAKE_PROFIT"
    assert result.reference_price == 110
    assert not result.ambiguous
    assert result.interval_end == START + timedelta(minutes=1)


def test_same_lower_bar_is_explicitly_ambiguous_and_stop_first():
    result = first_breach(window(candle(1, high=115, low=90)), D(95), D(110))
    assert result.trigger == "STOP_LOSS"
    assert result.reference_price == 95
    assert result.ambiguous
    assert not result.gap_at_open


def test_stop_gap_uses_observed_open_not_stop_price():
    result = first_breach(window(candle(1, 90, 115, 85, 100)), D(95), D(110))
    assert result.reference_price == 90
    assert result.gap_at_open
    assert not result.ambiguous


@pytest.mark.parametrize(
    "stop,target", [(D("NaN"), None), (D("Infinity"), None), (D(0), None), (D(110), D(95))]
)
def test_invalid_thresholds_rejected(stop, target):
    with pytest.raises(ValueError):
        first_breach(window(candle(1)), stop, target)


@pytest.mark.parametrize("mutation", ["gap", "parent", "duplicate", "partial", "overlap"])
def test_incomplete_intrabar_rejected_before_account_mutation(tmp_path, mutation):
    parent = candle(2)
    w = window(candle(1), candle(2))
    if mutation == "gap":
        w = replace(w, bars=(candle(2),))
    elif mutation == "parent":
        w = replace(w, bars=(candle(1, high=105), candle(2)))
    elif mutation == "overlap":
        w = replace(w, start=START - timedelta(minutes=1))
    windows = (w, w) if mutation == "duplicate" else (w,)
    parents = (candle(0), parent, candle(4)) if mutation == "partial" else (candle(0), parent)
    broker = PaperBrokerService(tmp_path / "invalid.db")
    with pytest.raises(ValueError):
        HistoricalReplayHarness(broker, plan()).run(
            HistoricalReplayDataset(parents, intrabar_windows=windows)
        )
    assert broker._connection.execute("SELECT COUNT(*) FROM paper_ledger").fetchone()[0] == 0


def test_new_entry_stops_inside_parent_and_retains_friction(tmp_path, monkeypatch):
    broker = PaperBrokerService(tmp_path / "entry.db")

    def enter(self, instrument, now, capital_plan, portfolio, **kwargs):
        fill = broker.buy(
            OrderIntent(
                "AAPL",
                Market.USA,
                Side.BUY,
                10,
                D(100),
                "synthetic-test",
                AssetClass.EQUITY,
                "backtest",
                D(95),
                D(110),
            )
        )
        return SimpleNamespace(execution=SimpleNamespace(fill=fill))

    monkeypatch.setattr(SwarmMarketAnalysisPipeline, "run", enter)
    parent = candle(2, high=115, low=90)
    result = HistoricalReplayHarness(broker, plan()).run(
        HistoricalReplayDataset(
            (candle(0), parent), intrabar_windows=(window(candle(1, low=90), candle(2, high=115)),)
        )
    )
    assert len(result.order_ids) == 2
    assert result.intrabar_exits[0]["trigger"] == "STOP_LOSS"
    assert result.intrabar_exits[0]["reference_price"] == "95"
    assert result.protection_model == "lower_timeframe_ohlc_stop_first"
    assert not broker.get_positions("backtest")
    assert result.final_snapshot.equity < D(100000) - D(50)
    assert broker._execution_time is None


def test_existing_gap_protection_precedes_discretionary_action(tmp_path, monkeypatch):
    broker = PaperBrokerService(tmp_path / "gap.db")
    broker.buy(
        OrderIntent(
            "AAPL",
            Market.USA,
            Side.BUY,
            10,
            D(100),
            "synthetic-test",
            AssetClass.EQUITY,
            "backtest",
            D(95),
            D(110),
        )
    )

    def forbidden(*args, **kwargs):
        pytest.fail("opening protective exit must suppress discretionary entry this parent")

    monkeypatch.setattr(SwarmMarketAnalysisPipeline, "run", forbidden)
    lower = candle(1, 90, 100, 85, 95)
    result = HistoricalReplayHarness(broker, plan()).run(
        HistoricalReplayDataset((candle(0), lower), intrabar_windows=(window(lower),))
    )
    assert len(result.order_ids) == 1
    assert result.intrabar_exits[0]["gap_at_open"]
    assert result.intrabar_exits[0]["reference_price"] == "90"
    assert not broker.get_positions("backtest")


def test_replay_exception_clears_historical_execution_context(tmp_path, monkeypatch):
    broker = PaperBrokerService(tmp_path / "failure.db")

    def fail(*args, **kwargs):
        raise RuntimeError("test failure")

    monkeypatch.setattr(SwarmMarketAnalysisPipeline, "run", fail)
    with pytest.raises(RuntimeError, match="test failure"):
        HistoricalReplayHarness(broker, plan()).run(HistoricalReplayDataset((candle(0), candle(1))))
    assert broker._execution_time is None
    assert broker._friction_context is None


def test_json_loader_and_tearsheet_expose_protection_model(tmp_path, monkeypatch):
    import json

    from quant_ai.backtesting.replay import load_replay_dataset
    from quant_ai.backtesting.tearsheet import build_tearsheet

    def row(bar):
        return {
            "timestamp": bar.timestamp.isoformat(),
            **{
                field: str(getattr(bar, field))
                for field in ("open", "high", "low", "close", "volume")
            },
        }

    source = tmp_path / "data.json"
    source.write_text(
        json.dumps(
            {
                "bars": [row(candle(0)), row(candle(1))],
                "intrabar_windows": [
                    {
                        "parent_timestamp": candle(1).timestamp.isoformat(),
                        "start": START.isoformat(),
                        "interval_seconds": 60,
                        "bars": [row(candle(1))],
                    }
                ],
            }
        )
    )
    dataset = load_replay_dataset(source, INSTRUMENT)
    monkeypatch.setattr(
        SwarmMarketAnalysisPipeline,
        "run",
        lambda *args, **kwargs: SimpleNamespace(execution=SimpleNamespace(fill=None)),
    )
    broker = PaperBrokerService(tmp_path / "json.db")
    harness = HistoricalReplayHarness(broker, plan())
    result = harness.run(dataset)
    report = json.loads(build_tearsheet(result, broker).to_json())
    assert report["protection_model"] == "lower_timeframe_ohlc_stop_first"
    assert report["intrabar_exits"] == []
    plain = harness.run(replace(dataset, intrabar_windows=()))
    assert (
        json.loads(build_tearsheet(plain, broker).to_json())["protection_model"] == "not_simulated"
    )
