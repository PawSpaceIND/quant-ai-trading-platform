"""Regression cover for the isolated logic findings 1, 3 and 8.

Finding 1 - the risk firewall treated every order as exposure-adding, so a SELL that
            unwound an over-concentrated position was rejected by the very caps it relieved.
Finding 3 - position size was a hardcoded quantity=10 in the CLI and ghost daemon while
            two correct, tested sizers went uncalled.
Finding 8 - the XAI proof never recorded the broker order id, so the UI could not join a
            rationale to the exact fill it produced ("No exact link").
"""
from __future__ import annotations

import asyncio
import inspect
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from quant_ai import cli
from quant_ai import daemon as ghost_daemon
from quant_ai.agents.atlas import AtlasInvestmentAgent
from quant_ai.agents.swarm import AgentAnalysisRequest, AtlasCIOAgent, TradeProposal
from quant_ai.agents.swarm_runtime import SwarmPaperTradingService
from quant_ai.domain.models import (
    AssetClass,
    Instrument,
    Market,
    OrderIntent,
    PortfolioSnapshot,
    RiskMode,
    Side,
)
from quant_ai.execution.audit import XAITraceLogger
from quant_ai.execution.daemon import AutonomousTradingDaemon
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.execution.portfolio import PortfolioTracker
from quant_ai.execution.scheduler import AutonomousCadenceScheduler
from quant_ai.intelligence.pipeline import SwarmMarketAnalysisPipeline
from quant_ai.intelligence.sandbox import (
    SandboxFundamentalDataProvider,
    SandboxMacroIndicatorProvider,
    SandboxNewsSentimentProvider,
)
from quant_ai.marketdata.feed import UsaSandboxMarketDataFeed
from quant_ai.planning.capital import CapitalGoalEngine, CapitalPlanRequest
from quant_ai.portfolio.sizing import PositionSizer
from quant_ai.risk.policy import RiskFirewall

TS = datetime(2026, 9, 14, 14, 0, tzinfo=timezone.utc)  # Monday, US regular hours
AAPL = Instrument("AAPL", Market.USA, AssetClass.EQUITY, "USD", "NASDAQ")


def _plan():
    return CapitalGoalEngine().recommend(CapitalPlanRequest(
        Decimal(100000), Decimal("0.80"), Decimal("0.20"),
        expected_edge=Decimal("0.02"), requested_mode=RiskMode.BALANCED))


def _order(side: Side, quantity: int, price: int = 200, stop: int | None = 196) -> OrderIntent:
    return OrderIntent("AAPL", Market.USA, side, quantity, Decimal(price), "test",
                       AssetClass.EQUITY, "ghost",
                       Decimal(stop) if stop is not None else None, None)


def _portfolio(exposure: int = 0, pnl: int = 0, equity: int = 100000) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        Decimal(equity), Decimal(pnl), Decimal(exposure), Decimal(100000),
        {"AAPL": Decimal(exposure)} if exposure else {},
        {AssetClass.EQUITY: Decimal(exposure)} if exposure else {},
    )


def _proposal(side: Side, quantity: int = 10) -> TradeProposal:
    return TradeProposal(
        uuid4().hex, "AAPL", Market.USA, "USA", AssetClass.EQUITY, side, quantity,
        Decimal(200), Decimal(196), Decimal(208), Decimal("0.80"), Decimal("0.02"),
        Decimal("0.01"), ("test",),
    )


def _request() -> AgentAnalysisRequest:
    return AgentAnalysisRequest("AAPL", Market.USA, AssetClass.EQUITY, TS, {})


def _pipeline(broker: PaperBrokerService, feed=None):
    feed = feed or UsaSandboxMarketDataFeed()
    runtime = SwarmPaperTradingService(
        cio=AtlasCIOAgent(AtlasInvestmentAgent()), broker=broker, xai_logger=XAITraceLogger())
    return SwarmMarketAnalysisPipeline(
        feed, SandboxNewsSentimentProvider(), SandboxFundamentalDataProvider(),
        SandboxMacroIndicatorProvider(), runtime=runtime), feed


# ----------------------------------------------------------------- Finding 1: directionality
def test_f1_full_unwind_of_an_over_concentrated_position_clears_the_firewall():
    """15% in one symbol against a 10% cap, and the unwind is 3x the single-trade cap.
    Every additive check would reject this; a de-risking SELL must not be."""
    decision = RiskFirewall().evaluate(_order(Side.SELL, 75, stop=None), _portfolio(exposure=15000))

    assert decision.approved
    assert decision.reason == "approved_risk_reducing"


def test_f1_adding_to_an_over_concentrated_position_is_still_rejected():
    decision = RiskFirewall().evaluate(_order(Side.BUY, 5), _portfolio(exposure=15000))

    assert not decision.approved
    assert decision.reason == "symbol_concentration_limit"


def test_f1_loss_halt_freezes_buying_but_not_selling():
    halted = _portfolio(exposure=2000, pnl=-3000)  # breaches the 2% daily-loss limit

    assert RiskFirewall().evaluate(_order(Side.BUY, 5), halted).reason == "daily_loss_limit_reached"
    assert RiskFirewall().evaluate(_order(Side.SELL, 10, stop=None), halted).approved


def test_f1_drawdown_halt_freezes_buying_but_not_selling():
    halted = PortfolioSnapshot(Decimal(89000), Decimal(0), Decimal(2000), Decimal(100000),
                               {"AAPL": Decimal(2000)}, {AssetClass.EQUITY: Decimal(2000)})

    assert RiskFirewall().evaluate(_order(Side.BUY, 5), halted).reason == "max_drawdown_reached"
    assert RiskFirewall().evaluate(_order(Side.SELL, 10, stop=None), halted).approved


def test_f1_a_de_risking_sell_needs_no_protective_stop():
    assert RiskFirewall().evaluate(_order(Side.SELL, 10, stop=None), _portfolio(exposure=2000)).approved


def test_f1_selling_beyond_the_holding_is_treated_as_exposure_adding():
    """Hold 5 shares, sell 50: 45 would be a short. That slice is subject to every cap."""
    decision = RiskFirewall().evaluate(_order(Side.SELL, 50, stop=204), _portfolio(exposure=1000))

    assert not decision.approved
    assert decision.reason == "single_trade_notional_limit"


def test_f1_partial_reduction_projects_exposure_downward():
    """Selling half of a 9% position must be judged at 4.5% projected, not 13.5%."""
    decision = RiskFirewall().evaluate(_order(Side.SELL, 22, stop=None), _portfolio(exposure=9000))

    assert decision.approved


def test_f1_buy_path_is_unchanged():
    assert RiskFirewall().evaluate(_order(Side.BUY, 10), _portfolio()).reason == "approved"
    assert RiskFirewall().evaluate(_order(Side.BUY, 10, stop=None), _portfolio()).reason == "protective_stop_required"


def test_f1_daemon_can_de_risk_when_over_concentrated_end_to_end():
    broker = PaperBrokerService(":memory:", starting_capital=Decimal(100000))
    runtime = SwarmPaperTradingService(broker=broker, xai_logger=XAITraceLogger())
    broker.buy(_order(Side.BUY, 10))
    over_concentrated = _portfolio(exposure=20000)  # the mark has run up to 20% of equity

    result = runtime._execute_proposal(
        _request(), (), _proposal(Side.SELL, 10), _plan(), over_concentrated, None, "ghost")

    assert result.fill is not None, result.risk_decision.reason
    assert result.fill.filled_quantity == 10
    assert broker.get_positions("ghost") == ()


# ----------------------------------------------------------------- Finding 3: dynamic sizing
def test_f3_size_follows_live_equity_not_starting_capital():
    sizer, plan = PositionSizer(), _plan()

    at_100k = sizer.quantity_from_plan(plan, _portfolio(equity=100000), Decimal(200))
    at_50k = sizer.quantity_from_plan(plan, _portfolio(equity=50000), Decimal(200))
    at_200k = sizer.quantity_from_plan(plan, _portfolio(equity=200000), Decimal(200))

    assert at_100k == 25          # 5% single-trade cap binds: 5,000 / 200
    assert at_50k == 12           # halves with equity (12.5 -> 12)
    assert at_200k == 50          # and doubles with it
    assert at_100k != 10, "the hardcoded constant must be gone"


def test_f3_size_respects_every_cap_simultaneously():
    sizer, plan = PositionSizer(), _plan()
    portfolio = _portfolio(equity=100000)
    price = Decimal(200)

    quantity = sizer.quantity_from_plan(plan, portfolio, price)
    notional = Decimal(quantity) * price

    assert notional <= portfolio.equity * plan.per_trade_risk_fraction / plan.stop_loss_fraction
    assert notional <= portfolio.equity * plan.max_position_fraction
    assert notional <= portfolio.equity * sizer.policy.max_trade_fraction
    assert RiskFirewall().evaluate(
        _order(Side.BUY, quantity), portfolio).approved, "a sized entry must clear the firewall"


def test_f3_size_shrinks_as_capital_is_deployed_elsewhere():
    sizer, plan = PositionSizer(), _plan()
    # BALANCED keeps a 20% cash reserve: 80,000 deployable. 79,000 already committed.
    portfolio = PortfolioSnapshot(Decimal(100000), Decimal(0), Decimal(79000))

    assert sizer.quantity_from_plan(plan, portfolio, Decimal(200)) == 5


def test_f3_size_is_zero_when_nothing_is_deployable():
    sizer, plan = PositionSizer(), _plan()
    fully_deployed = PortfolioSnapshot(Decimal(100000), Decimal(0), Decimal(80000))

    assert sizer.quantity_from_plan(plan, fully_deployed, Decimal(200)) == 0


def test_f3_pipeline_sizes_dynamically_when_no_quantity_is_given():
    broker = PaperBrokerService(":memory:", starting_capital=Decimal(100000))
    pipeline, _feed = _pipeline(broker)
    portfolio = _portfolio(equity=100000)

    result = pipeline.run(AAPL, TS, _plan(), portfolio, country="USA", tenant_id="ghost")
    price = result.execution.proposal.reference_price

    assert result.requested_quantity > 0
    assert result.requested_quantity != 10
    assert Decimal(result.requested_quantity) * price <= portfolio.equity * Decimal("0.05")
    assert result.execution.fill is not None, result.execution.risk_decision.reason


def test_f3_an_explicit_quantity_is_still_honoured():
    broker = PaperBrokerService(":memory:", starting_capital=Decimal(100000))
    pipeline, _feed = _pipeline(broker)

    result = pipeline.run(AAPL, TS, _plan(), _portfolio(), quantity=7, country="USA", tenant_id="ghost")

    assert result.requested_quantity == 7


def test_f3_unaffordable_entry_is_never_rounded_up_by_conflict_halving():
    assert SwarmMarketAnalysisPipeline._apply_conflict(0, Decimal("0.50")) == 0
    assert SwarmMarketAnalysisPipeline._apply_conflict(1, Decimal("0.50")) == 1
    assert SwarmMarketAnalysisPipeline._apply_conflict(24, Decimal("0.50")) == 12
    assert SwarmMarketAnalysisPipeline._apply_conflict(24, Decimal("0.10")) == 24


def test_f3_daemon_entry_is_sized_from_capital_not_a_constant():
    broker = PaperBrokerService(":memory:", starting_capital=Decimal(100000))
    pipeline, feed = _pipeline(broker)
    scheduler = AutonomousCadenceScheduler(pipeline, cadence=timedelta(minutes=10))
    tracker = PortfolioTracker(broker, feed, tenant_id="ghost")
    daemon = AutonomousTradingDaemon(scheduler, tracker, AAPL, _plan(), country="USA", tenant_id="ghost")

    assert daemon.quantity is None
    asyncio.run(daemon.run_once(TS))

    position = broker.get_positions("ghost")[0]
    assert position.quantity != 10
    assert position.quantity > 0
    assert Decimal(position.quantity) * position.average_price <= Decimal(100000) * Decimal("0.05") * Decimal("1.01")


def test_f3_cli_and_ghost_runner_no_longer_hardcode_the_share_count():
    assert "quantity=10" not in inspect.getsource(cli)
    assert "quantity=10" not in inspect.getsource(ghost_daemon)


# ----------------------------------------------------------------- Finding 8: proof <-> fill link
def test_f8_a_filled_proof_carries_the_exact_broker_order_id(tmp_path):
    broker = PaperBrokerService(":memory:", starting_capital=Decimal(100000))
    runtime = SwarmPaperTradingService(broker=broker, xai_logger=XAITraceLogger(tmp_path))
    proposal = _proposal(Side.BUY)

    result = runtime._execute_proposal(_request(), (), proposal, _plan(), _portfolio(), None, "ghost")

    assert result.fill is not None
    assert result.xai_trace.order_id == result.fill.order_id
    assert broker.ledger_entries("ghost")[0].order_id == result.xai_trace.order_id

    on_disk = json.loads((tmp_path / f"{proposal.decision_id}.json").read_text())
    assert on_disk["order_id"] == result.fill.order_id
    assert isinstance(on_disk["order_id"], str), "the UI joins on typeof order_id === 'string'"


def test_f8_a_rejected_proof_claims_no_order(tmp_path):
    broker = PaperBrokerService(":memory:", starting_capital=Decimal(100000))
    runtime = SwarmPaperTradingService(broker=broker, xai_logger=XAITraceLogger(tmp_path))
    runtime.kill_switch.engage("drill")
    proposal = _proposal(Side.BUY)

    result = runtime._execute_proposal(_request(), (), proposal, _plan(), _portfolio(), None, "ghost")

    assert result.fill is None
    assert result.xai_trace.order_id is None
    assert json.loads((tmp_path / f"{proposal.decision_id}.json").read_text())["order_id"] is None


def test_f8_markdown_report_names_the_order(tmp_path):
    broker = PaperBrokerService(":memory:", starting_capital=Decimal(100000))
    runtime = SwarmPaperTradingService(broker=broker, xai_logger=XAITraceLogger(tmp_path))
    proposal = _proposal(Side.BUY)

    result = runtime._execute_proposal(_request(), (), proposal, _plan(), _portfolio(), None, "ghost")

    assert f"- Order: {result.fill.order_id}" in (tmp_path / f"{proposal.decision_id}.md").read_text()


def test_f8_existing_trace_json_shape_is_preserved(tmp_path):
    """Additive only: every field the UI already read is still present."""
    broker = PaperBrokerService(":memory:", starting_capital=Decimal(100000))
    runtime = SwarmPaperTradingService(broker=broker, xai_logger=XAITraceLogger(tmp_path))
    proposal = _proposal(Side.BUY)

    runtime._execute_proposal(_request(), (), proposal, _plan(), _portfolio(), None, "ghost")
    payload = json.loads((tmp_path / f"{proposal.decision_id}.json").read_text())

    assert {"decision_id", "input_matrix", "confidence_distribution", "proposal",
            "declared_rationales", "stress_verdict", "risk_verdict", "order_id"} <= payload.keys()
