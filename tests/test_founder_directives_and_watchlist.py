from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from quant_ai.agents.atlas import AtlasInvestmentAgent, _atlas_prompt
from quant_ai.agents.contracts import AgentDomain, AgentEvidence, Stance
from quant_ai.agents.swarm import AgentAnalysisRequest, TradeProposal
from quant_ai.agents.swarm_runtime import SwarmPaperTradingService
from quant_ai.daemon import build_ghost_runner
from quant_ai.domain.models import (
    AssetClass,
    Instrument,
    Market,
    OrderIntent,
    PortfolioSnapshot,
    RiskMode,
    Side,
)
from quant_ai.execution.daemon import AutonomousTradingDaemon
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.execution.portfolio import PortfolioTracker
from quant_ai.execution.scheduler import AutonomousCadenceScheduler
from quant_ai.execution.session import MarketState
from quant_ai.governance.directives import FounderDirectives, country_for
from quant_ai.intelligence.pipeline import SwarmMarketAnalysisPipeline
from quant_ai.intelligence.sandbox import (
    SandboxFundamentalDataProvider,
    SandboxMacroIndicatorProvider,
    SandboxNewsSentimentProvider,
)
from quant_ai.marketdata.live_feed import LiveTickMarketDataFeed
from quant_ai.marketdata.ticker_stream import LiveTick, TickBuffer
from quant_ai.planning.capital import CapitalGoalEngine
from quant_ai.risk.warden import RiskWarden

NOW = datetime(2026, 9, 14, 14, tzinfo=timezone.utc)
NIFTY = Instrument("NIFTY", Market.INDIA, AssetClass.INDEX, "INR", "NSE")
AAPL = Instrument("AAPL", Market.USA, AssetClass.EQUITY, "USD", "NASDAQ")

DIRECTIVES = {
    "starting_capital": 250000,
    "risk_mode": "CONSERVATIVE",
    "allowed_markets": ["INDIA", "USA"],
    "allowed_asset_classes": ["EQUITY", "INDEX", "METAL", "FX", "COMMODITY"],
    "max_open_positions": 3,
    "watchlist": [
        {"symbol": "NIFTY", "market": "INDIA", "asset_class": "INDEX", "currency": "INR", "exchange": "NSE"},
        {"symbol": "GOLD", "market": "INDIA", "asset_class": "METAL", "currency": "INR", "exchange": "MCX",
         # An MCX row is a dated contract, so the directives file has to name which one.
         "expiry": "2026-12-05", "lot_size": 100, "tick_size": "1"},
        {"symbol": "USDINR", "market": "INDIA", "asset_class": "FX", "currency": "INR", "exchange": "CDS",
         # Currency derivatives are dated too: USDINR is a monthly contract, 1000 USD a lot.
         "expiry": "2026-12-29", "lot_size": 1000, "tick_size": "0.0025"},
        {"symbol": "AAPL", "market": "USA", "asset_class": "EQUITY", "currency": "USD", "exchange": "NASDAQ"},
    ],
    "instructions": "Preserve capital first. Prefer liquid, large instruments.",
}


# ---------------------------------------------------------------- directives

def test_directives_parse_capital_scope_watchlist_and_instructions() -> None:
    directives = FounderDirectives.from_json(DIRECTIVES)
    assert directives.starting_capital == Decimal(250000)
    assert directives.risk_mode == RiskMode.CONSERVATIVE
    assert [item.symbol for item in directives.watchlist] == ["NIFTY", "GOLD", "USDINR", "AAPL"]
    assert directives.watchlist[1].asset_class == AssetClass.METAL
    assert AssetClass.CRYPTO in directives.blocked_asset_classes()
    assert AssetClass.METAL not in directives.blocked_asset_classes()
    plan = CapitalGoalEngine().recommend(directives.capital_plan_request())
    assert plan.recommended_mode == RiskMode.CONSERVATIVE
    assert plan.starting_capital == Decimal(250000)
    assert country_for(directives.watchlist[0]) == "India"
    assert country_for(directives.watchlist[3]) == "USA"


def test_directives_reject_out_of_scope_and_duplicate_instruments() -> None:
    with pytest.raises(ValueError, match="asset class CRYPTO is not allowed"):
        FounderDirectives.from_json({
            "allowed_asset_classes": ["EQUITY"],
            "watchlist": [{"symbol": "BTC", "market": "USA", "asset_class": "CRYPTO"}],
        })
    with pytest.raises(ValueError, match="market INDIA is not allowed"):
        FounderDirectives.from_json({
            "allowed_markets": ["USA"],
            "watchlist": [{"symbol": "NIFTY", "market": "INDIA", "asset_class": "INDEX"}],
        })
    with pytest.raises(ValueError, match="duplicate watchlist symbol"):
        FounderDirectives.from_json({"watchlist": [
            {"symbol": "GOLD", "market": "INDIA", "asset_class": "METAL"},
            {"symbol": "GOLD", "market": "USA", "asset_class": "COMMODITY"},
        ]})
    with pytest.raises(ValueError, match="max_open_positions"):
        FounderDirectives(max_open_positions=0)


def test_directives_load_from_env_inline_or_file(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("PRAMANA_FOUNDER_DIRECTIVES_JSON", raising=False)
    monkeypatch.delenv("PRAMANA_FOUNDER_DIRECTIVES_FILE", raising=False)
    assert FounderDirectives.from_env() is None
    monkeypatch.setenv("PRAMANA_FOUNDER_DIRECTIVES_JSON", json.dumps({"risk_mode": None, "max_open_positions": 2}))
    inline = FounderDirectives.from_env()
    assert inline is not None and inline.risk_mode is None and inline.max_open_positions == 2
    monkeypatch.delenv("PRAMANA_FOUNDER_DIRECTIVES_JSON")
    location = tmp_path / "directives.json"
    location.write_text(json.dumps(DIRECTIVES), encoding="utf-8")
    monkeypatch.setenv("PRAMANA_FOUNDER_DIRECTIVES_FILE", str(location))
    loaded = FounderDirectives.from_env()
    assert loaded is not None and loaded.instructions.startswith("Preserve capital")


# ---------------------------------------------------------------- scope enforcement

def _proposal(asset_class: AssetClass, symbol: str = "GOLD") -> TradeProposal:
    return TradeProposal(
        "d", symbol, Market.INDIA, "India", asset_class, Side.BUY, 1, Decimal(100),
        Decimal(95), Decimal(110), Decimal("0.8"), Decimal("0.02"), Decimal("0.01"), ("t",),
    )


def test_warden_refuses_asset_classes_the_founder_blocked() -> None:
    plan = CapitalGoalEngine().recommend(FounderDirectives().capital_plan_request())
    snapshot = PortfolioSnapshot(Decimal(100000), Decimal(0), Decimal(0), peak_equity=Decimal(100000))
    warden = RiskWarden(blocked_asset_classes=(AssetClass.METAL,))
    assert warden.evaluate(_proposal(AssetClass.METAL), plan, snapshot).reason == "asset_class_blocked"
    assert warden.evaluate(_proposal(AssetClass.EQUITY, "RELIANCE"), plan, snapshot).approved


def _buy_evidence(symbol: str) -> tuple[AgentEvidence, ...]:
    return tuple(
        AgentEvidence(
            f"agent-{index}", AgentDomain.TECHNICAL, symbol, Stance.STRONG_BUY,
            Decimal("0.8"), Decimal("0.02"), Decimal("0.01"), ("declared",), NOW, 0,
        )
        for index in range(4)
    )


def test_runtime_caps_concurrent_open_positions(tmp_path) -> None:
    broker = PaperBrokerService(tmp_path / "cap.db", starting_capital=Decimal(100000), slippage_bps=Decimal(0))
    for symbol in ("RELIANCE", "TCS"):
        broker.buy(OrderIntent(symbol, Market.INDIA, Side.BUY, 5, Decimal(100), "seed", AssetClass.EQUITY, "cap", Decimal(95), Decimal(110)))
    plan = CapitalGoalEngine().recommend(FounderDirectives().capital_plan_request())
    snapshot = PortfolioSnapshot(
        Decimal(100000), Decimal(0), Decimal(1000), peak_equity=Decimal(100000),
        symbol_exposure={"RELIANCE": Decimal(500), "TCS": Decimal(500)},
        asset_exposure={AssetClass.EQUITY: Decimal(1000)},
        symbol_quantity={"RELIANCE": 5, "TCS": 5},
    )
    request = AgentAnalysisRequest("INFY", Market.INDIA, AssetClass.EQUITY, NOW, {}, 0)

    capped = SwarmPaperTradingService(broker=broker, max_open_positions=2).execute(
        request, _buy_evidence("INFY"), plan, snapshot, quantity=5, reference_price=Decimal(100),
        stop_price=Decimal(95), take_profit_price=Decimal(110), country="India", tenant_id="cap",
    )
    assert capped.fill is None
    assert capped.risk_decision.reason == "max_open_positions_reached"

    roomy = SwarmPaperTradingService(broker=broker, max_open_positions=3).execute(
        request, _buy_evidence("INFY"), plan, snapshot, quantity=5, reference_price=Decimal(100),
        stop_price=Decimal(95), take_profit_price=Decimal(110), country="India", tenant_id="cap",
    )
    assert roomy.fill is not None
    assert len(broker.get_positions("cap")) == 3


def test_founder_instructions_reach_the_prompt_and_every_proof() -> None:
    text = "Preserve capital first; never add risk into a falling market."
    prompt = _atlas_prompt("NIFTY", _buy_evidence("NIFTY"), None, text)
    assert f"founder_directives={text}" in prompt
    assert "execution_mode=PAPER_ONLY" in prompt
    decision = AtlasInvestmentAgent(founder_instructions=text).decide("NIFTY", _buy_evidence("NIFTY"), NOW)
    assert any(item.startswith("founder_directives=") for item in decision.rationale)
    assert not any(item.startswith("founder_directives=") for item in AtlasInvestmentAgent().decide("NIFTY", _buy_evidence("NIFTY"), NOW).rationale)


# ---------------------------------------------------------------- watchlist daemon

def _live_daemon(tmp_path, instruments, now):
    buffer = TickBuffer()
    feed = LiveTickMarketDataFeed(buffer, clock=lambda: now)
    broker = PaperBrokerService(tmp_path / "watch.db", starting_capital=Decimal(100000), slippage_bps=Decimal(0))
    pipeline = SwarmMarketAnalysisPipeline(
        feed, SandboxNewsSentimentProvider(), SandboxFundamentalDataProvider(), SandboxMacroIndicatorProvider(),
        runtime=SwarmPaperTradingService(broker=broker),
    )
    plan = CapitalGoalEngine().recommend(FounderDirectives().capital_plan_request())
    daemon = AutonomousTradingDaemon(
        AutonomousCadenceScheduler(pipeline), PortfolioTracker(broker, feed, tenant_id="watch"),
        instruments[0], plan, country=country_for(instruments[0]), tenant_id="watch",
        instruments=instruments, clock=lambda: now,
    )
    return daemon, broker, buffer


def test_daemon_evaluates_every_watchlist_instrument_and_charges_country_exposure(tmp_path) -> None:
    # Monday 10:30 IST: NSE is in regular hours, New York is closed.
    now = datetime(2026, 9, 14, 5, 0, 30, tzinfo=timezone.utc)
    daemon, broker, buffer = _live_daemon(tmp_path, (NIFTY, AAPL), now)
    start = now - timedelta(minutes=60, seconds=30)
    for minute in range(61):
        at = start + timedelta(minutes=minute)
        buffer.put(LiveTick("NIFTY", Decimal(24000 + minute), Decimal(1000 * minute), None, None, at, "test"))
        buffer.put(LiveTick("RELIANCE", Decimal(24000 + minute), Decimal(1000 * minute), None, None, at, "test"))
        buffer.put(LiveTick("AAPL", Decimal(220), Decimal(1000 * minute), None, None, at, "test"))
    # The seeded holding is an NSE share, not the index the watchlist is briefed on. An
    # index cannot be bought - you buy a future or an ETF on it - and the friction model
    # now refuses to price one rather than charging it the cash-equity schedule.
    broker.buy(OrderIntent("RELIANCE", Market.INDIA, Side.BUY, 2, Decimal(24000), "seed", AssetClass.EQUITY, "watch", Decimal(23500), Decimal(24500)))

    brief = asyncio.run(daemon.run_once(now))

    assert [item.subject for item in daemon.briefs] == ["NIFTY", "AAPL"]
    assert daemon.briefs[0].market_state == MarketState.REGULAR_HOURS
    assert daemon.briefs[1].market_state == MarketState.CLOSED
    assert brief is daemon.briefs[0]
    exposure = daemon.country_exposure(now)
    assert exposure["India"] == Decimal(24060) * 2  # marked from the live tick, not a constant
    assert "USA" not in exposure
    assert daemon.notifications.pending("watch")[-1].metadata["market_state"] == "REGULAR_HOURS"


def test_ghost_runner_builds_from_founder_directives(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_LIVE_MONEY_ACTIVE", "false")
    directives = FounderDirectives.from_json(DIRECTIVES)
    runner = build_ghost_runner(
        zerodha_api_key="k", zerodha_access_token="t", zerodha_instrument_tokens=(1, 2, 3),
        zerodha_symbol_by_token={1: "NIFTY", 2: "GOLD", 3: "USDINR"}, ib_client=SimpleNamespace(),
        ib_contracts=(), database=tmp_path / "paper.db", log_path=tmp_path / "ghost.log",
        include_ibkr=False, directives=directives,
    )
    daemon = runner.daemon
    assert [item.symbol for item in daemon.instruments] == ["NIFTY", "GOLD", "USDINR", "AAPL"]
    assert daemon.country == "India"
    assert daemon.tracker.broker.get_margin("ghost").starting_capital == Decimal(250000)
    assert daemon.plan.recommended_mode == RiskMode.CONSERVATIVE
    runtime = daemon.scheduler.pipeline.runtime
    assert runtime.max_open_positions == 3
    assert AssetClass.CRYPTO in runtime.warden.blocked_asset_classes
    assert runtime.cio.atlas.founder_instructions.startswith("Preserve capital")
