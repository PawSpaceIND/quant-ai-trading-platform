from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from quant_ai.agents.contracts import Stance
from quant_ai.agents.swarm import AgentAnalysisRequest, TechnicalQuantAgent, USEquitiesAgent
from quant_ai.cli import main as cli_main
from quant_ai.daemon import build_ghost_runner
from quant_ai.domain.models import AssetClass, Instrument, Market, RiskMode
from quant_ai.execution.daemon import OPERATOR_HALT_PREFIX, AutonomousTradingDaemon
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.execution.portfolio import PortfolioTracker
from quant_ai.execution.scheduler import AutonomousCadenceScheduler
from quant_ai.execution.session import (
    GlobalVenue,
    MarketCalendar,
    MarketState,
    default_holidays,
    holidays_from_json,
)
from quant_ai.intelligence.pipeline import SwarmMarketAnalysisPipeline
from quant_ai.intelligence.providers import NewsSignal
from quant_ai.intelligence.sandbox import (
    SandboxFundamentalDataProvider,
    SandboxMacroIndicatorProvider,
    SandboxNewsSentimentProvider,
)
from quant_ai.marketdata.feed import UsaSandboxMarketDataFeed
from quant_ai.marketdata.live_feed import LiveTickMarketDataFeed, TickBarAggregator
from quant_ai.marketdata.ticker_stream import LiveTick, TickBuffer
from quant_ai.planning.capital import CapitalGoalEngine, CapitalPlanRequest

T0 = datetime(2026, 9, 14, 9, 30, tzinfo=timezone.utc)
NIFTY = Instrument("NIFTY", Market.INDIA, AssetClass.INDEX, "INR", "NSE")
GOLD = Instrument("GOLD", Market.INDIA, AssetClass.METAL, "INR", "MCX")


def tick(symbol: str, at: datetime, ltp: str, volume: str = "0", bid: str | None = None, ask: str | None = None) -> LiveTick:
    return LiveTick(
        symbol, Decimal(ltp), Decimal(volume),
        Decimal(bid) if bid else None, Decimal(ask) if ask else None, at, "test",
    )


# ---------------------------------------------------------------- live feed

def test_ticks_roll_into_closed_one_minute_bars_and_the_forming_bar_stays_hidden() -> None:
    buffer = TickBuffer()
    now = [T0 + timedelta(minutes=2, seconds=30)]
    feed = LiveTickMarketDataFeed(buffer, clock=lambda: now[0])
    for seconds, price, volume in ((5, "100", "1000"), (20, "104", "1500"), (40, "98", "1800")):
        buffer.put(tick("NIFTY", T0 + timedelta(seconds=seconds), price, volume))
    for seconds, price, volume in ((65, "99", "2000"), (90, "101", "2600")):
        buffer.put(tick("NIFTY", T0 + timedelta(seconds=seconds), price, volume))
    buffer.put(tick("NIFTY", T0 + timedelta(seconds=125), "150", "3000"))  # still forming

    candles = feed.fetch_ohlcv(NIFTY, T0 - timedelta(hours=1), now[0])
    assert [c.timestamp for c in candles] == [T0 + timedelta(minutes=1), T0 + timedelta(minutes=2)]
    first, second = candles
    assert (first.open, first.high, first.low, first.close) == (Decimal(100), Decimal(104), Decimal(98), Decimal(98))
    assert first.volume == Decimal(800)  # deltas 0 + 500 + 300; the first tick has no baseline
    assert (second.open, second.close, second.volume) == (Decimal(99), Decimal(101), Decimal(800))
    assert all(c.instrument == NIFTY for c in candles)

    now[0] = T0 + timedelta(minutes=3)
    assert len(feed.fetch_ohlcv(NIFTY, T0 - timedelta(hours=1), now[0])) == 3


def test_feed_is_market_agnostic_and_marks_from_the_latest_tick() -> None:
    buffer = TickBuffer()
    feed = LiveTickMarketDataFeed(buffer, clock=lambda: T0 + timedelta(minutes=1))
    buffer.put(tick("GOLD", T0, "72500", "10", bid="72495", ask="72505"))
    mark = feed.latest_tick(GOLD)
    assert (mark.last_price, mark.bid, mark.ask) == (Decimal(72500), Decimal(72495), Decimal(72505))
    assert feed.fetch_ohlcv(GOLD, T0 - timedelta(hours=1), T0 + timedelta(minutes=1)) != ()
    with pytest.raises(ValueError, match="no_live_tick"):
        feed.latest_tick(NIFTY)


def test_stale_and_crossed_quotes_cannot_be_used_as_fresh_marks() -> None:
    buffer = TickBuffer()
    feed = LiveTickMarketDataFeed(buffer, clock=lambda: T0 + timedelta(days=2), max_tick_age=timedelta(hours=24))
    buffer.put(tick("NIFTY", T0, "100"))
    with pytest.raises(ValueError, match="stale_live_tick"):
        feed.latest_tick(NIFTY)
    fresh = LiveTickMarketDataFeed(TickBuffer(), clock=lambda: T0)
    fresh.buffer.put(tick("NIFTY", T0, "100", bid="101", ask="99"))
    with pytest.raises(ValueError, match="no_live_tick"):
        fresh.latest_tick(NIFTY)
    assert fresh.buffer.integrity()["rejected"] == {"crossed_tick_quotes": 1}


def test_aggregator_ignores_late_ticks_and_bounds_history() -> None:
    aggregator = TickBarAggregator(max_bars=2)
    for minute in range(4):
        aggregator.ingest(tick("NIFTY", T0 + timedelta(minutes=minute), str(100 + minute)))
    aggregator.ingest(tick("NIFTY", T0 + timedelta(minutes=1, seconds=30), "1"))  # late: ignored
    bars = aggregator.closed_bars("NIFTY", T0 - timedelta(hours=1), T0 + timedelta(hours=1), T0 + timedelta(minutes=5))
    assert [bar.close for bar in bars] == [Decimal(102), Decimal(103)]
    assert aggregator.latest_close("NIFTY", T0 + timedelta(minutes=5)) == Decimal(103)


# ---------------------------------------------------------------- ghost wiring

def test_ghost_runner_uses_live_ticks_for_candles_and_marks_in_any_market(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_LIVE_MONEY_ACTIVE", "false")
    runner = build_ghost_runner(
        zerodha_api_key="k", zerodha_access_token="t", zerodha_instrument_tokens=(1,),
        zerodha_symbol_by_token={1: "NIFTY"}, ib_client=SimpleNamespace(), ib_contracts=(),
        database=tmp_path / "paper.db", log_path=tmp_path / "ghost.log", instrument=NIFTY,
        include_ibkr=False,
    )
    feed = runner.daemon.tracker.market_feed
    assert isinstance(feed, LiveTickMarketDataFeed)
    assert runner.daemon.scheduler.pipeline.market_feed is feed
    # An Indian instrument no longer trips the hardcoded USA feed.
    assert feed.fetch_ohlcv(NIFTY, T0 - timedelta(hours=1), T0) == ()
    assert runner.daemon.scheduler.calendar.holidays[GlobalVenue.INDIA]


# ---------------------------------------------------------------- holidays

def test_shipped_holiday_calendar_closes_observed_and_fixed_dates() -> None:
    calendar = MarketCalendar(holidays=default_holidays())
    assert calendar.state(Market.USA, datetime(2026, 7, 3, 15, tzinfo=timezone(timedelta(hours=-4)))) == MarketState.CLOSED
    assert calendar.state(Market.USA, datetime(2026, 12, 25, 10, tzinfo=timezone(timedelta(hours=-5)))) == MarketState.CLOSED
    assert calendar.state(Market.INDIA, datetime(2026, 1, 26, 11, tzinfo=timezone(timedelta(hours=5, minutes=30)))) == MarketState.CLOSED
    assert calendar.state(Market.INDIA, datetime(2026, 1, 27, 11, tzinfo=timezone(timedelta(hours=5, minutes=30)))) == MarketState.REGULAR_HOURS


def test_holidays_from_json_merges_over_the_shipped_calendar() -> None:
    merged = holidays_from_json({"INDIA": ["2026-11-09"], "usa": ["2026-06-19"]}, default_holidays())
    assert date(2026, 11, 9) in merged[GlobalVenue.INDIA]
    assert date(2026, 1, 26) in merged[GlobalVenue.INDIA]
    assert date(2026, 6, 19) in merged[GlobalVenue.USA]


# ---------------------------------------------------------------- operator halt

def _daemon(tmp_path, halt_file):
    broker = PaperBrokerService(tmp_path / "halt.db", starting_capital=Decimal(100000), slippage_bps=Decimal(0))
    feed = UsaSandboxMarketDataFeed()
    pipeline = SwarmMarketAnalysisPipeline(
        feed, SandboxNewsSentimentProvider(), SandboxFundamentalDataProvider(), SandboxMacroIndicatorProvider(),
    )
    plan = CapitalGoalEngine().recommend(CapitalPlanRequest(
        Decimal(100000), Decimal("0.8"), Decimal("0.2"), expected_edge=Decimal("0.02"), requested_mode=RiskMode.BALANCED,
    ))
    return AutonomousTradingDaemon(
        AutonomousCadenceScheduler(pipeline), PortfolioTracker(broker, feed, tenant_id="ops"),
        Instrument("AAPL", Market.USA, AssetClass.EQUITY, "USD", "NASDAQ"), plan,
        country="USA", tenant_id="ops", halt_file=halt_file,
    )


def test_halt_file_engages_and_its_removal_releases_the_kill_switch(tmp_path) -> None:
    halt = tmp_path / "PRAMANA_HALT"
    daemon = _daemon(tmp_path, halt)
    asyncio.run(daemon.run_once(T0))
    assert not daemon.kill_switch.engaged

    halt.write_text("founder pause for review", encoding="utf-8")
    asyncio.run(daemon.run_once(T0 + timedelta(minutes=10)))
    assert daemon.kill_switch.engaged
    assert daemon.kill_switch.reason == f"{OPERATOR_HALT_PREFIX}: founder pause for review"

    halt.unlink()
    asyncio.run(daemon.run_once(T0 + timedelta(minutes=20)))
    assert not daemon.kill_switch.engaged
    assert any("kill_switch_released" in str(event) for event in daemon.audit.events())


def test_a_latched_cadence_halt_is_not_released_by_the_halt_file(tmp_path) -> None:
    halt = tmp_path / "PRAMANA_HALT"
    daemon = _daemon(tmp_path, halt)
    daemon.engage_kill_switch("cadence failed 5 times consecutively: upstream down")
    asyncio.run(daemon.run_once(T0))
    assert daemon.kill_switch.engaged


def test_cli_halt_and_resume_manage_the_marker_file(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("PRAMANA_LEDGER_PATH", str(tmp_path / "operator-test.sqlite"))
    monkeypatch.setenv("PRAMANA_TENANT_ID", "operator-test")
    marker = tmp_path / "state" / "PRAMANA_HALT"
    monkeypatch.setenv("PRAMANA_HALT_FILE", str(marker))
    assert cli_main(["halt", "--reason", "weekend maintenance"]) == 0
    assert marker.read_text(encoding="utf-8") == "weekend maintenance"
    assert cli_main(["resume"]) == 0
    assert not marker.exists()
    assert cli_main(["resume"]) == 0
    assert "no halt file present" in capsys.readouterr().out


# ---------------------------------------------------------------- agents & metrics

def test_valuation_agent_abstains_on_non_equity_instruments() -> None:
    request = AgentAnalysisRequest("GOLD", Market.USA, AssetClass.METAL, T0, {"pe": Decimal(0), "us10y": Decimal(4)}, 0)
    evidence = USEquitiesAgent().analyze(request)
    assert evidence.stance == Stance.NEUTRAL
    assert "non_equity_instrument" in evidence.rationale
    equity = AgentAnalysisRequest("AAPL", Market.USA, AssetClass.EQUITY, T0, {"pe": Decimal(0), "us10y": Decimal(4)}, 0)
    assert "non_equity_instrument" not in USEquitiesAgent().analyze(equity).rationale


def test_technical_agent_abstains_below_fifty_bars_and_momentum_is_ten_bars() -> None:
    thin = SwarmMarketAnalysisPipeline._technical_metrics(tuple(Decimal(100 + i) for i in range(49)))
    assert thin["price_history_bars"] == Decimal(49)
    request = AgentAnalysisRequest("NIFTY", Market.INDIA, AssetClass.INDEX, T0, thin, 0)
    assert "insufficient_price_history" in TechnicalQuantAgent().analyze(request).rationale

    closes = tuple(Decimal(100) for _ in range(60))
    closes = closes[:-11] + (Decimal(100),) + tuple(Decimal(110) for _ in range(10))
    full = SwarmMarketAnalysisPipeline._technical_metrics(closes)
    assert full["price_history_bars"] == Decimal(60)
    assert full["momentum"] == Decimal("0.1")  # 110 vs the close ten bars earlier (100)


def test_news_sentiment_only_counts_headlines_inside_the_window() -> None:
    pipeline = SwarmMarketAnalysisPipeline(
        UsaSandboxMarketDataFeed(), SandboxNewsSentimentProvider(),
        SandboxFundamentalDataProvider(), SandboxMacroIndicatorProvider(),
        news_window=timedelta(hours=6),
    )
    items = (
        NewsSignal("AAPL", "old", Decimal("0.9"), "t", T0 - timedelta(days=2)),
        NewsSignal("AAPL", "new", Decimal("-0.5"), "t", T0 - timedelta(hours=1)),
    )
    assert pipeline._recent_sentiment(items, T0) == Decimal("-0.5")
    assert pipeline._recent_sentiment((), T0) == Decimal(0)
