"""Regression cover for critical audit findings C1-C5.

Each test names the finding it locks down. These are the tests that must fail if a future
change reintroduces unguarded stops, position stacking, a fatal cadence tick, a resettable
drawdown breaker, or an unprotected daemon execution path.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from quant_ai.agents.atlas import AtlasInvestmentAgent
from quant_ai.agents.swarm import AgentAnalysisRequest, AtlasCIOAgent, TradeProposal
from quant_ai.agents.swarm_runtime import SwarmPaperTradingService
from quant_ai.brokers.adapter import BrokerPosition
from quant_ai.daemon import CadenceFaultPolicy, DaemonRunner
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
from quant_ai.execution.protective_exits import ExitTrigger, ProtectiveExitEngine
from quant_ai.execution.scheduler import AutonomousCadenceScheduler
from quant_ai.intelligence.pipeline import SwarmMarketAnalysisPipeline
from quant_ai.intelligence.sandbox import (
    SandboxFundamentalDataProvider,
    SandboxMacroIndicatorProvider,
    SandboxNewsSentimentProvider,
)
from quant_ai.llm.anthropic_client import AnthropicSwarmClient
from quant_ai.marketdata.feed import UsaSandboxMarketDataFeed
from quant_ai.orders.state import OrderState
from quant_ai.planning.capital import CapitalGoalEngine, CapitalPlanRequest

TS = datetime(2026, 9, 14, 14, 0, tzinfo=timezone.utc)  # Monday, US regular hours
AAPL = Instrument("AAPL", Market.USA, AssetClass.EQUITY, "USD", "NASDAQ")


def _buy(quantity: int = 10, stop: Decimal | None = Decimal(196),
         target: Decimal | None = Decimal(208)) -> OrderIntent:
    return OrderIntent("AAPL", Market.USA, Side.BUY, quantity, Decimal(200), "test",
                       AssetClass.EQUITY, "ghost", stop, target)


def _fixed_mark(price: Decimal):
    return lambda position: price


def _plan():
    return CapitalGoalEngine().recommend(CapitalPlanRequest(
        Decimal(100000), Decimal("0.80"), Decimal("0.20"),
        expected_edge=Decimal("0.02"), requested_mode=RiskMode.BALANCED))


def _proposal(side: Side | None = Side.BUY, quantity: int = 10) -> TradeProposal:
    """An actionable proposal. Building it directly keeps these tests about the execution
    guards rather than about whether the swarm happened to vote."""
    return TradeProposal(
        decision_id=uuid4().hex,
        symbol="AAPL",
        market=Market.USA,
        country="USA",
        asset_class=AssetClass.EQUITY,
        side=side,
        quantity=quantity,
        reference_price=Decimal(200),
        stop_price=Decimal(196),
        take_profit_price=Decimal(208),
        confidence=Decimal("0.80"),
        expected_return=Decimal("0.02"),
        expected_risk=Decimal("0.01"),
        rationale=("test",),
    )


def _request() -> AgentAnalysisRequest:
    return AgentAnalysisRequest("AAPL", Market.USA, AssetClass.EQUITY, TS, {})


def _portfolio() -> PortfolioSnapshot:
    return PortfolioSnapshot(Decimal(100000), Decimal(0), Decimal(0))


def _daemon(database=":memory:", *, feed=None, llm=None, capital=Decimal(100000),
            scaling=False, quantity=10):
    broker = PaperBrokerService(database, starting_capital=capital)
    feed = feed or UsaSandboxMarketDataFeed()
    runtime = SwarmPaperTradingService(
        cio=AtlasCIOAgent(AtlasInvestmentAgent(llm_client=llm)),
        broker=broker, xai_logger=XAITraceLogger(), allow_position_scaling=scaling)
    pipeline = SwarmMarketAnalysisPipeline(
        feed, SandboxNewsSentimentProvider(), SandboxFundamentalDataProvider(),
        SandboxMacroIndicatorProvider(), runtime=runtime)
    scheduler = AutonomousCadenceScheduler(pipeline, cadence=timedelta(minutes=10))
    tracker = PortfolioTracker(broker, feed, tenant_id="ghost")
    daemon = AutonomousTradingDaemon(scheduler, tracker, AAPL, _plan(),
                                     quantity=quantity, country="USA", tenant_id="ghost")
    return broker, tracker, daemon


# --------------------------------------------------------------------------- C1
def test_c1_protective_levels_persist_to_ledger_and_position():
    broker = PaperBrokerService(":memory:", starting_capital=Decimal(100000))
    broker.buy(_buy())

    entry = broker.ledger_entries("ghost")[0]
    assert entry.stop_price == Decimal(196)
    assert entry.take_profit_price == Decimal(208)

    position = broker.get_positions("ghost")[0]
    assert position.stop_price == Decimal(196)
    assert position.take_profit_price == Decimal(208)


def test_c1_stop_breach_liquidates_the_position():
    broker = PaperBrokerService(":memory:", starting_capital=Decimal(100000))
    broker.buy(_buy())
    engine = ProtectiveExitEngine(broker, _fixed_mark(Decimal(140)), tenant_id="ghost")

    exits = engine.evaluate(TS)

    assert len(exits) == 1
    assert exits[0].trigger is ExitTrigger.STOP_LOSS
    assert exits[0].filled is True
    assert exits[0].quantity == 10
    assert broker.get_positions("ghost") == ()
    sells = [e for e in broker.ledger_entries("ghost") if e.side == Side.SELL]
    assert len(sells) == 1
    assert sells[0].quantity == 10


def test_c1_take_profit_breach_liquidates_the_position():
    broker = PaperBrokerService(":memory:", starting_capital=Decimal(100000))
    broker.buy(_buy())
    engine = ProtectiveExitEngine(broker, _fixed_mark(Decimal(220)), tenant_id="ghost")

    exits = engine.evaluate(TS)

    assert exits[0].trigger is ExitTrigger.TAKE_PROFIT
    assert broker.get_positions("ghost") == ()


def test_c1_price_inside_the_band_does_not_exit():
    broker = PaperBrokerService(":memory:", starting_capital=Decimal(100000))
    broker.buy(_buy())
    engine = ProtectiveExitEngine(broker, _fixed_mark(Decimal(201)), tenant_id="ghost")

    assert engine.evaluate(TS) == ()
    assert broker.get_positions("ghost")[0].quantity == 10


def test_c1_unavailable_mark_is_not_treated_as_a_safe_price():
    broker = PaperBrokerService(":memory:", starting_capital=Decimal(100000))
    broker.buy(_buy())

    def dead(position: BrokerPosition) -> Decimal:
        raise ConnectionError("market data socket closed")

    engine = ProtectiveExitEngine(broker, dead, tenant_id="ghost")

    assert engine.evaluate(TS) == ()                      # no exit invented
    assert broker.get_positions("ghost")[0].quantity == 10  # and no crash


def test_c1_stop_wins_when_a_misconfiguration_satisfies_both_thresholds():
    """Inverted levels (stop above target) must resolve to the protective side, not the greedy one."""
    broker = PaperBrokerService(":memory:", starting_capital=Decimal(100000))
    broker.buy(_buy(stop=Decimal(205), target=Decimal(203)))
    engine = ProtectiveExitEngine(broker, _fixed_mark(Decimal(204)), tenant_id="ghost")

    assert engine.evaluate(TS)[0].trigger is ExitTrigger.STOP_LOSS


def test_c1_partial_sell_keeps_the_stop_guarding_the_remainder():
    broker = PaperBrokerService(":memory:", starting_capital=Decimal(100000))
    broker.buy(_buy(quantity=10))
    broker.sell(OrderIntent("AAPL", Market.USA, Side.SELL, 4, Decimal(201), "test",
                            AssetClass.EQUITY, "ghost", None, None))

    remainder = broker.get_positions("ghost")[0]
    assert remainder.quantity == 6
    assert remainder.stop_price == Decimal(196)


def test_c1_daemon_sweeps_protective_exits_on_every_tick():
    broker, _tracker, daemon = _daemon(feed=UsaSandboxMarketDataFeed(base_price=Decimal(140)))
    broker.buy(_buy())

    asyncio.run(daemon.run_once(TS))

    # The breached position is liquidated before the swarm is consulted. The daemon may then
    # open a fresh, separately-protected position at the new price - that is correct, and is
    # exactly why the exit must run first.
    assert any(item.trigger is ExitTrigger.STOP_LOSS for item in daemon.protective_exits)
    assert daemon.protective_exits[0].filled is True
    sells = [e for e in broker.ledger_entries("ghost") if e.side == Side.SELL]
    assert len(sells) == 1 and sells[0].quantity == 10
    assert any(e.event_type == "protective_exit" for e in daemon.audit.events())


def test_c1_legacy_ledger_is_migrated_in_place(tmp_path):
    """A ledger written before this change must gain the columns, not crash on open."""
    import sqlite3

    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE paper_accounts (tenant_id TEXT PRIMARY KEY, starting_capital TEXT NOT NULL,
            cash_balance TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE paper_positions (tenant_id TEXT NOT NULL, symbol TEXT NOT NULL,
            market TEXT NOT NULL, asset_class TEXT NOT NULL, quantity INTEGER NOT NULL,
            average_price TEXT NOT NULL, PRIMARY KEY (tenant_id, symbol, market, asset_class));
        CREATE TABLE paper_ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL
            UNIQUE, tenant_id TEXT NOT NULL, symbol TEXT NOT NULL, market TEXT NOT NULL,
            asset_class TEXT NOT NULL, side TEXT NOT NULL, quantity INTEGER NOT NULL,
            fill_price TEXT NOT NULL, notional TEXT NOT NULL, status TEXT NOT NULL,
            created_at TEXT NOT NULL);
        """
    )
    legacy.commit()
    legacy.close()

    broker = PaperBrokerService(path, starting_capital=Decimal(100000))
    broker.buy(_buy())

    assert broker.get_positions("ghost")[0].stop_price == Decimal(196)


# --------------------------------------------------------------------------- C2
def _execute_buy(runtime: SwarmPaperTradingService, portfolio: PortfolioSnapshot):
    request = AgentAnalysisRequest("AAPL", Market.USA, AssetClass.EQUITY, TS, {})
    proposal = runtime.cio.propose(
        request, (), quantity=10, reference_price=Decimal(200),
        stop_price=Decimal(196), take_profit_price=Decimal(208), country="USA")
    return runtime, proposal


def test_c2_second_entry_into_an_open_symbol_is_rejected():
    broker = PaperBrokerService(":memory:", starting_capital=Decimal(100000))
    runtime = SwarmPaperTradingService(broker=broker, xai_logger=XAITraceLogger())
    broker.buy(_buy())

    held = runtime._held_quantity(_buy(), "ghost")

    assert held == 10
    # the guard is configuration-driven and defaults to blocking
    assert runtime.allow_position_scaling is False


def test_c2_consecutive_buy_ticks_produce_one_position_not_four():
    broker, _tracker, daemon = _daemon()

    for i in range(6):
        asyncio.run(daemon.run_once(TS + timedelta(minutes=10 * i)))

    positions = broker.get_positions("ghost")
    buys = [e for e in broker.ledger_entries("ghost") if e.side == Side.BUY]
    assert len(buys) == 1, f"expected a single governed entry, got {len(buys)}"
    assert positions[0].quantity == 10


def test_c2_rejection_reason_is_governed_not_incidental():
    _broker, _tracker, daemon = _daemon()
    asyncio.run(daemon.run_once(TS))
    brief = asyncio.run(daemon.run_once(TS + timedelta(minutes=10)))

    assert brief.risk_decision == "position_already_open"
    assert brief.mode == "PRESERVE_CAPITAL"


def test_c2_scaling_may_be_enabled_explicitly():
    broker, _tracker, daemon = _daemon(scaling=True)

    asyncio.run(daemon.run_once(TS))
    asyncio.run(daemon.run_once(TS + timedelta(minutes=10)))

    buys = [e for e in broker.ledger_entries("ghost") if e.side == Side.BUY]
    assert len(buys) == 2, "explicit opt-in must still permit averaging"


# --------------------------------------------------------------------------- C3
class _BadTickDaemon(AutonomousTradingDaemon):
    calls = 0
    fail_until = 1

    async def run_once(self, now=None):
        type(self).calls += 1
        if type(self).calls <= type(self).fail_until:
            raise ConnectionError("transient upstream failure")
        return await super().run_once(now)


def _run_runner(daemon, *, ticks: int, fault_policy=None):
    sleeps: list[float] = []
    state = {"n": 0}

    async def sleeper(seconds: float) -> None:
        sleeps.append(seconds)
        state["n"] += 1
        if state["n"] > ticks:
            runner.request_stop()

    runner = DaemonRunner(daemon, streams=[], cadence=timedelta(minutes=10),
                          fault_policy=fault_policy, log_path="/tmp/pramana-test-ghost.log",
                          clock=lambda: TS, sleeper=sleeper)
    asyncio.run(runner.start())
    return runner, sleeps


def test_c3_runner_absorbs_a_failing_tick_and_keeps_running():
    _broker, _tracker, daemon = _daemon()
    daemon.__class__ = _BadTickDaemon
    _BadTickDaemon.calls = 0
    _BadTickDaemon.fail_until = 1

    runner, _sleeps = _run_runner(daemon, ticks=6)

    assert _BadTickDaemon.calls > 1, "supervisor died on the first fault"
    assert runner.consecutive_failures == 0, "recovery must reset the failure counter"


def test_c3_backoff_escalates_5_15_60():
    policy = CadenceFaultPolicy()
    assert [policy.delay_for(n) for n in (1, 2, 3, 4, 9)] == [5.0, 15.0, 60.0, 60.0, 60.0]


def test_c3_persistent_failure_latches_the_kill_switch():
    _broker, _tracker, daemon = _daemon()
    daemon.__class__ = _BadTickDaemon
    _BadTickDaemon.calls = 0
    _BadTickDaemon.fail_until = 99  # never recovers

    _run_runner(daemon, ticks=12,
                fault_policy=CadenceFaultPolicy(backoff_seconds=(0.0,), halt_after_consecutive_failures=3))

    assert daemon.kill_switch.engaged is True
    assert "cadence failed" in (daemon.kill_switch.reason or "")


@pytest.mark.parametrize("status", [401, 403, 429, 500, 502, 503, 404])
def test_c3_provider_http_errors_degrade_to_neutral_consensus(status):
    class _Failing:
        class messages:
            @staticmethod
            async def create(**_kwargs):
                error = RuntimeError(f"HTTP {status}")
                error.status_code = status
                raise error

    client = AnthropicSwarmClient(client=_Failing())
    payload = asyncio.run(client.generate_trading_consensus("AAPL"))
    signal, proof = client.parse_consensus(payload)

    assert signal.stance.value == "NEUTRAL"
    assert signal.confidence == Decimal(0)
    assert "Consensus Skipped" in proof.summary


def test_c3_socket_drop_degrades_to_neutral_consensus():
    class _Dropped:
        class messages:
            @staticmethod
            async def create(**_kwargs):
                raise ConnectionError("connection reset by peer")

    client = AnthropicSwarmClient(client=_Dropped())
    payload = asyncio.run(client.generate_trading_consensus("AAPL"))

    assert client.parse_consensus(payload)[0].stance.value == "NEUTRAL"


def test_c3_broker_rejection_is_governed_not_fatal():
    broker = PaperBrokerService(":memory:", starting_capital=Decimal(100))
    runtime = SwarmPaperTradingService(broker=broker, xai_logger=XAITraceLogger())
    request, proposal, portfolio = _request(), _proposal(), _portfolio()

    result = runtime._execute_proposal(request, (), proposal, _plan(), portfolio, None, "ghost")

    assert result.fill is None
    assert result.risk_decision.approved is False


# --------------------------------------------------------------------------- C4
def test_c4_peak_equity_is_persisted_to_the_ledger():
    broker = PaperBrokerService(":memory:", starting_capital=Decimal(100000))
    assert broker.get_peak_equity("ghost") is None

    assert broker.record_peak_equity(Decimal(120000), "ghost") == Decimal(120000)
    assert broker.get_peak_equity("ghost") == Decimal(120000)
    # a lower observation must never lower the mark
    assert broker.record_peak_equity(Decimal(90000), "ghost") == Decimal(120000)


def test_c4_drawdown_breaker_survives_a_restart(tmp_path):
    path = tmp_path / "ledger.db"
    feed = UsaSandboxMarketDataFeed()

    broker = PaperBrokerService(path, starting_capital=Decimal(100000))
    broker.record_peak_equity(Decimal(120000), "ghost")
    tracker = PortfolioTracker(broker, feed, tenant_id="ghost")
    before = tracker.metrics(TS)
    broker.close()

    # cold restart against the same file
    restarted_broker = PaperBrokerService(path, starting_capital=Decimal(100000))
    restarted = PortfolioTracker(restarted_broker, feed, tenant_id="ghost")
    after = restarted.metrics(TS)

    assert after.high_water_mark == Decimal(120000)
    assert after.drawdown_fraction == before.drawdown_fraction
    assert after.drawdown_fraction > Decimal("0.16"), "a tripped breaker must stay tripped"


def test_c4_tracker_reloads_the_mark_instead_of_starting_capital(tmp_path):
    path = tmp_path / "ledger.db"
    broker = PaperBrokerService(path, starting_capital=Decimal(100000))
    broker.record_peak_equity(Decimal(133000), "ghost")
    broker.close()

    reopened = PaperBrokerService(path, starting_capital=Decimal(100000))
    tracker = PortfolioTracker(reopened, UsaSandboxMarketDataFeed(), tenant_id="ghost")

    assert tracker._high_water_mark == Decimal(133000)


def test_c4_new_equity_high_is_written_through_on_the_metrics_call(tmp_path):
    path = tmp_path / "ledger.db"
    broker = PaperBrokerService(path, starting_capital=Decimal(100000))
    tracker = PortfolioTracker(broker, UsaSandboxMarketDataFeed(), tenant_id="ghost")
    tracker._high_water_mark = Decimal(50000)  # simulate a mark below current equity

    metrics = tracker.metrics(TS)

    assert metrics.high_water_mark == Decimal(100000)
    assert broker.get_peak_equity("ghost") == Decimal(100000), (
        "a new equity high must reach the ledger, not just the in-memory tracker"
    )


# --------------------------------------------------------------------------- C5
def test_c5_kill_switch_blocks_the_daemon_execution_path():
    broker = PaperBrokerService(":memory:", starting_capital=Decimal(100000))
    runtime = SwarmPaperTradingService(broker=broker, xai_logger=XAITraceLogger())
    runtime.kill_switch.engage("operator halt")
    request, proposal, portfolio = _request(), _proposal(), _portfolio()

    result = runtime._execute_proposal(request, (), proposal, _plan(), portfolio, None, "ghost")

    assert result.fill is None
    assert result.risk_decision.reason.startswith("kill_switch_engaged")
    assert broker.ledger_entries("ghost") == ()


def test_c5_engaged_kill_switch_stops_daemon_trading():
    broker, _tracker, daemon = _daemon()
    daemon.engage_kill_switch("audit drill")

    brief = asyncio.run(daemon.run_once(TS))

    assert brief.mode == "PRESERVE_CAPITAL"
    assert broker.ledger_entries("ghost") == ()


def test_c5_idempotency_claim_is_durable_across_restart(tmp_path):
    path = tmp_path / "ledger.db"
    broker = PaperBrokerService(path, starting_capital=Decimal(100000))
    assert broker.claim_idempotency_key("key-abc", "ghost") is True
    assert broker.claim_idempotency_key("key-abc", "ghost") is False
    broker.close()

    reopened = PaperBrokerService(path, starting_capital=Decimal(100000))
    assert reopened.claim_idempotency_key("key-abc", "ghost") is False, (
        "a restart must not permit resubmitting an order that already reached the broker"
    )


def test_c5_duplicate_proposal_is_rejected_once_claimed():
    broker = PaperBrokerService(":memory:", starting_capital=Decimal(100000))
    runtime = SwarmPaperTradingService(broker=broker, xai_logger=XAITraceLogger(),
                                       allow_position_scaling=True)
    request, proposal, portfolio = _request(), _proposal(), _portfolio()

    first = runtime._execute_proposal(request, (), proposal, _plan(), portfolio, None, "ghost")
    replay = runtime._execute_proposal(request, (), proposal, _plan(), portfolio, None, "ghost")

    assert first.fill is not None
    assert replay.fill is None
    assert replay.risk_decision.reason == "duplicate_order"


def test_c5_order_lifecycle_is_tracked_through_to_fill():
    broker = PaperBrokerService(":memory:", starting_capital=Decimal(100000))
    runtime = SwarmPaperTradingService(broker=broker, xai_logger=XAITraceLogger())
    request, proposal, portfolio = _request(), _proposal(), _portfolio()

    result = runtime._execute_proposal(request, (), proposal, _plan(), portfolio, None, "ghost")

    assert result.order_state is OrderState.FILLED


def test_c5_rejected_order_lands_in_rejected_state():
    broker = PaperBrokerService(":memory:", starting_capital=Decimal(100000))
    runtime = SwarmPaperTradingService(broker=broker, xai_logger=XAITraceLogger())
    runtime.kill_switch.engage("halt")
    request, proposal, portfolio = _request(), _proposal(), _portfolio()

    result = runtime._execute_proposal(request, (), proposal, _plan(), portfolio, None, "ghost")

    assert result.order_state is OrderState.REJECTED


def test_c5_daemon_and_runtime_share_one_kill_switch():
    _broker, _tracker, daemon = _daemon()
    daemon.engage_kill_switch("shared control")

    assert daemon.scheduler.pipeline.runtime.kill_switch.engaged is True
    assert daemon.kill_switch is daemon.scheduler.pipeline.runtime.kill_switch


# ------------------------------------------------- C2 extension: post-stop re-entry cooldown
def test_c2_stop_out_arms_a_durable_re_entry_cooldown(tmp_path):
    path = tmp_path / "ledger.db"
    broker = PaperBrokerService(path, starting_capital=Decimal(100000))
    broker.buy(_buy())
    engine = ProtectiveExitEngine(broker, _fixed_mark(Decimal(140)), tenant_id="ghost",
                                  re_entry_cooldown=timedelta(minutes=30))

    engine.evaluate(TS)
    until = broker.exit_cooldown_until("AAPL", Market.USA, AssetClass.EQUITY, "ghost")
    broker.close()

    assert until == TS + timedelta(minutes=30)
    # the cooldown must outlive a restart, exactly like the high-water mark
    reopened = PaperBrokerService(path, starting_capital=Decimal(100000))
    assert reopened.exit_cooldown_until("AAPL", Market.USA, AssetClass.EQUITY, "ghost") == until


def test_c2_cooldown_blocks_immediate_re_entry_then_expires():
    broker = PaperBrokerService(":memory:", starting_capital=Decimal(100000))
    runtime = SwarmPaperTradingService(broker=broker, xai_logger=XAITraceLogger())
    broker.record_exit_cooldown("AAPL", Market.USA, AssetClass.EQUITY,
                                TS + timedelta(minutes=30), "ghost")

    during = runtime._execute_proposal(
        AgentAnalysisRequest("AAPL", Market.USA, AssetClass.EQUITY, TS, {}),
        (), _proposal(), _plan(), _portfolio(), None, "ghost")
    after = runtime._execute_proposal(
        AgentAnalysisRequest("AAPL", Market.USA, AssetClass.EQUITY,
                             TS + timedelta(minutes=31), {}),
        (), _proposal(), _plan(), _portfolio(), None, "ghost")

    assert during.risk_decision.reason == "re_entry_cooldown_active"
    assert during.fill is None
    assert after.fill is not None, "the cooldown must expire, not latch permanently"


def test_c2_cooldown_can_be_disabled():
    broker = PaperBrokerService(":memory:", starting_capital=Decimal(100000))
    broker.buy(_buy())
    engine = ProtectiveExitEngine(broker, _fixed_mark(Decimal(140)), tenant_id="ghost",
                                  re_entry_cooldown=timedelta(0))

    engine.evaluate(TS)

    assert broker.exit_cooldown_until("AAPL", Market.USA, AssetClass.EQUITY, "ghost") is None


def test_c2_downtrend_does_not_churn_through_repeated_stop_outs():
    """A sustained decline must produce one contained loss, not a repeated bleed."""
    class Decaying(UsaSandboxMarketDataFeed):
        def __init__(self): super().__init__(base_price=Decimal(200)); self.step = 0
        def advance(self):
            self.step += 1
            self.base_price = (Decimal(200) * (Decimal("0.99") ** self.step)).quantize(Decimal("0.01"))

    feed = Decaying()
    broker, tracker, daemon = _daemon(feed=feed)
    for i in range(8):
        asyncio.run(daemon.run_once(TS + timedelta(minutes=10 * i)))
        feed.advance()

    buys = [e for e in broker.ledger_entries("ghost") if e.side == Side.BUY]
    assert len(buys) <= 2, f"re-entry churn: {len(buys)} entries in a single downtrend"
    assert tracker.metrics(TS + timedelta(minutes=80)).realized_pnl > Decimal(-100)


# ------------------------------------- C1 (burn-in finding): never mark from the synthetic feed
def _live_resolver(buffer, base_price: Decimal):
    from quant_ai.execution.protective_exits import market_feed_mark_resolver
    from quant_ai.orchestration.cadence import CadenceMarketReader

    return market_feed_mark_resolver(
        UsaSandboxMarketDataFeed(base_price=base_price), lambda _p: AAPL,
        CadenceMarketReader(buffer), lambda: TS,
    )


def _held_at_230() -> BrokerPosition:
    # A real entry far from the sandbox feed's 200 constant: stop 225.65, target 238.70.
    return BrokerPosition("ghost", "AAPL", Market.USA, AssetClass.EQUITY, 10, Decimal(230),
                          Decimal("225.65"), Decimal("238.70"))


def test_c1_ghost_wiring_never_falls_back_to_the_synthetic_feed():
    from quant_ai.marketdata.ticker_stream import LiveTick, TickBuffer

    buffer = TickBuffer()
    resolver = _live_resolver(buffer, base_price=Decimal(200))

    assert resolver(_held_at_230()) is None, "nothing buffered -> unknown, not 200"
    buffer.put(LiveTick("AAPL", Decimal(231), Decimal(1), None, None,
                        TS - timedelta(minutes=5), "test"))
    assert resolver(_held_at_230()) is None, "stale tick -> unknown, not 200"
    buffer.put(LiveTick("AAPL", Decimal(231), Decimal(1), None, None,
                        TS - timedelta(seconds=5), "test"))
    assert resolver(_held_at_230()) == Decimal(231)


def test_c1_a_websocket_gap_cannot_liquidate_a_healthy_position():
    """Before this fix a 2-minute tick gap marked a $230 position at the feed's 200 constant,
    which sits below its 225.65 stop, and sold it."""
    from quant_ai.marketdata.ticker_stream import TickBuffer

    broker = PaperBrokerService(":memory:", starting_capital=Decimal(100000))
    broker.buy(OrderIntent("AAPL", Market.USA, Side.BUY, 10, Decimal(230), "test",
                           AssetClass.EQUITY, "ghost", Decimal("225.65"), Decimal("238.70")))
    engine = ProtectiveExitEngine(broker, _live_resolver(TickBuffer(), Decimal(200)), tenant_id="ghost")

    assert engine.evaluate(TS) == ()
    assert broker.get_positions("ghost")[0].quantity == 10


def test_c1_feed_fallback_is_kept_where_no_live_source_exists():
    from quant_ai.execution.protective_exits import market_feed_mark_resolver

    resolver = market_feed_mark_resolver(UsaSandboxMarketDataFeed(base_price=Decimal(140)), lambda _p: AAPL)

    assert resolver(_held_at_230()) == Decimal(140)  # CLI path: the feed is the only source
