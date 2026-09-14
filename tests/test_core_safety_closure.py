from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from quant_ai.agents.swarm import AgentAnalysisRequest, TradeProposal
from quant_ai.agents.swarm_runtime import SwarmPaperTradingService
from quant_ai.backtest.costs import CostModel
from quant_ai.backtest.replay import ReplayEngine
from quant_ai.backtesting.replay import HistoricalReplayDataset, HistoricalReplayHarness
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
from quant_ai.execution.risk_state import SQLiteRiskStateStore
from quant_ai.intelligence.pipeline import SwarmMarketAnalysisPipeline
from quant_ai.intelligence.providers import MacroSnapshot
from quant_ai.intelligence.regime import (
    MarketRegime,
    MarketRegimeDetector,
    RegimeAssessment,
)
from quant_ai.intelligence.sandbox import (
    SandboxFundamentalDataProvider,
    SandboxMacroIndicatorProvider,
    SandboxNewsSentimentProvider,
)
from quant_ai.marketdata.feed import UsaSandboxMarketDataFeed
from quant_ai.marketdata.models import Candle
from quant_ai.planning.capital import CapitalGoalEngine, CapitalPlanRequest
from quant_ai.portfolio.sizing import PositionSizer
from quant_ai.risk.policy import RiskFirewall, RiskPolicy
from quant_ai.risk.warden import RiskWarden
from quant_ai.strategies.base import StrategySignal

INSTRUMENT = Instrument("AAPL", Market.USA, AssetClass.EQUITY, "USD", "NASDAQ")
NOW = datetime(2026, 9, 14, 14, 0, tzinfo=timezone.utc)


def plan():
    return CapitalGoalEngine().recommend(
        CapitalPlanRequest(
            Decimal(100000),
            Decimal("0.8"),
            Decimal("0.2"),
            expected_edge=Decimal("0.02"),
            requested_mode=RiskMode.BALANCED,
        )
    )


def buy_order(*, target: Decimal = Decimal(110)) -> OrderIntent:
    return OrderIntent(
        "AAPL",
        Market.USA,
        Side.BUY,
        10,
        Decimal(100),
        "audit",
        AssetClass.EQUITY,
        "default",
        Decimal(95),
        target,
    )


def held_portfolio(quantity: int = 10) -> PortfolioSnapshot:
    exposure = Decimal(100 * quantity)
    return PortfolioSnapshot(
        Decimal(100000),
        Decimal(0),
        exposure,
        Decimal(100000),
        symbol_exposure={"AAPL": exposure},
        asset_exposure={AssetClass.EQUITY: exposure},
        symbol_quantity={"AAPL": quantity},
        country_exposure={"USA": exposure},
    )


def test_mtm_daily_loss_halts_new_risk_and_realized_compatibility_remains() -> None:
    firewall = RiskFirewall()
    mtm_loss = PortfolioSnapshot(
        Decimal(100000),
        Decimal(0),
        Decimal(0),
        Decimal(100000),
        daily_total_pnl=Decimal(-3000),
    )
    assert firewall.evaluate(buy_order(), mtm_loss).reason == "daily_loss_limit_reached"

    realized_loss = PortfolioSnapshot(Decimal(100000), Decimal(-3000), Decimal(0))
    assert firewall.evaluate(buy_order(), realized_loss).reason == "daily_loss_limit_reached"


def test_take_profit_must_be_on_profit_side() -> None:
    flat = PortfolioSnapshot(Decimal(100000), Decimal(0), Decimal(0))
    assert RiskFirewall().evaluate(buy_order(target=Decimal(90)), flat).reason == "take_profit_wrong_side"


def test_country_limit_never_blocks_pure_covered_exit() -> None:
    held = PortfolioSnapshot(
        Decimal(100000),
        Decimal(0),
        Decimal(50000),
        Decimal(100000),
        symbol_exposure={"AAPL": Decimal(50000)},
        asset_exposure={AssetClass.EQUITY: Decimal(50000)},
        symbol_quantity={"AAPL": 500},
        country_exposure={"USA": Decimal(50000)},
    )
    sell = TradeProposal(
        "exit",
        "AAPL",
        Market.USA,
        "USA",
        AssetClass.EQUITY,
        Side.SELL,
        100,
        Decimal(100),
        Decimal(105),
        Decimal(90),
        Decimal(1),
        Decimal(0),
        Decimal(0),
        ("de-risk",),
    )
    decision = RiskWarden().evaluate(sell, plan(), held)
    assert decision.approved, decision.reason

    buy = TradeProposal(
        "add",
        "MSFT",
        Market.USA,
        "USA",
        AssetClass.EQUITY,
        Side.BUY,
        10,
        Decimal(1000),
        Decimal(950),
        Decimal(1100),
        Decimal(1),
        Decimal(0),
        Decimal(0),
        ("add",),
    )
    assert RiskWarden().evaluate(buy, plan(), held).reason == "country_allocation_limit"


def test_watchlist_country_metadata_survives_ledger_reconstruction() -> None:
    custom = Instrument(
        "ACME",
        Market.GLOBAL,
        AssetClass.EQUITY,
        "USD",
        "GLOBAL",
        metadata={"country": "Singapore"},
    )
    daemon = object.__new__(AutonomousTradingDaemon)
    daemon.instruments = (INSTRUMENT, custom)
    daemon.instrument = INSTRUMENT
    daemon.country = "USA"
    reconstructed = Instrument(
        "ACME", Market.GLOBAL, AssetClass.EQUITY, "USD", "GLOBAL"
    )

    assert daemon.country_of(reconstructed) == "Singapore"


def test_blocked_asset_class_never_traps_a_covered_exit() -> None:
    firewall = RiskFirewall(RiskPolicy(blocked_asset_classes=(AssetClass.EQUITY,)))
    sell = OrderIntent(
        "AAPL",
        Market.USA,
        Side.SELL,
        10,
        Decimal(100),
        "exit",
        AssetClass.EQUITY,
        "default",
        Decimal(105),
        Decimal(90),
    )
    assert firewall.evaluate(sell, held_portfolio()).approved
    assert firewall.evaluate(buy_order(), held_portfolio()).reason == "asset_class_blocked"


def test_kill_switch_never_traps_a_covered_exit() -> None:
    broker = PaperBrokerService(":memory:", starting_capital=Decimal(100000))
    broker.buy(buy_order())
    runtime = SwarmPaperTradingService(broker=broker, xai_logger=XAITraceLogger())
    runtime.kill_switch.engage("operator halt")
    proposal = TradeProposal(
        "covered-exit",
        "AAPL",
        Market.USA,
        "USA",
        AssetClass.EQUITY,
        Side.SELL,
        10,
        Decimal(100),
        Decimal(105),
        Decimal(90),
        Decimal(1),
        Decimal(0),
        Decimal(0),
        ("de-risk",),
    )
    request = AgentAnalysisRequest("AAPL", Market.USA, AssetClass.EQUITY, NOW, {})

    result = runtime._execute_proposal(
        request,
        (),
        proposal,
        plan(),
        held_portfolio(),
        {"USA": Decimal(1000)},
        "default",
    )

    assert result.fill is not None
    assert broker.get_positions("default") == ()


def test_file_backed_risk_state_survives_restart_and_rolls_day(tmp_path) -> None:
    path = tmp_path / "risk.db"
    day1 = date(2026, 9, 14)
    day2 = date(2026, 9, 15)

    first = SQLiteRiskStateStore(path, starting_capital=Decimal(100000))
    assert first.record_equity("default", day1, Decimal(98000)) == Decimal(98000)
    assert first.record_equity("default", day1, Decimal(97000)) == Decimal(98000)
    first.set_kill_switch("default", True, "persistent fault")
    first.close()

    second = SQLiteRiskStateStore(path, starting_capital=Decimal(100000))
    assert second.kill_switch_state("default") == (True, "persistent fault")
    assert second.record_equity("default", day1, Decimal(96000)) == Decimal(98000)
    assert second.record_equity("default", day2, Decimal(95000)) == Decimal(96000)
    second.close()


def test_cli_resume_clears_persisted_breaker(tmp_path, monkeypatch) -> None:
    from quant_ai.cli import main as cli_main

    ledger = tmp_path / "ledger.sqlite"
    state = SQLiteRiskStateStore(ledger)
    state.set_kill_switch("ghost", True, "cadence fault")
    state.close()

    monkeypatch.setenv("PRAMANA_LEDGER_PATH", str(ledger))
    monkeypatch.setenv("PRAMANA_TENANT_ID", "ghost")
    assert cli_main(["resume"]) == 0

    reopened = SQLiteRiskStateStore(ledger)
    assert reopened.kill_switch_state("ghost") == (False, None)
    reopened.close()


def test_fill_aware_sizing_respects_risk_budget_after_adverse_fill() -> None:
    capital_plan = plan()
    portfolio = PortfolioSnapshot(Decimal(100000), Decimal(0), Decimal(0))
    worst = Decimal(103)
    quantity = PositionSizer().quantity_from_plan(
        capital_plan,
        portfolio,
        Decimal(100),
        worst_entry_price=worst,
    )
    stop = Decimal(100) * (Decimal(1) - capital_plan.stop_loss_fraction)
    assert (worst - stop) * quantity <= Decimal(100000) * capital_plan.per_trade_risk_fraction
    assert worst * quantity <= Decimal(100000) * Decimal("0.05")


def test_target_preserves_reward_risk_after_worst_bounded_fill() -> None:
    pipeline = SwarmMarketAnalysisPipeline(
        UsaSandboxMarketDataFeed(),
        SandboxNewsSentimentProvider(),
        SandboxFundamentalDataProvider(),
        SandboxMacroIndicatorProvider(),
    )
    capital_plan = plan()
    reference = Decimal(100)
    stop, target = pipeline._protective_levels(capital_plan, reference)
    worst = pipeline.runtime.broker.friction_model.worst_case_execution_price(
        reference, Side.BUY
    )

    assert stop is not None and target is not None
    assert target > worst
    reward_distance = target - worst
    expected_reward = (worst - stop) * capital_plan.reward_risk_ratio
    assert abs(reward_distance - expected_reward) <= Decimal("1e-24")


def test_macro_changes_are_observation_to_observation_not_fixed_anchors() -> None:
    pipeline = SwarmMarketAnalysisPipeline(
        UsaSandboxMarketDataFeed(),
        SandboxNewsSentimentProvider(),
        SandboxFundamentalDataProvider(),
        SandboxMacroIndicatorProvider(),
    )
    first = MacroSnapshot({"US10Y": Decimal(4), "BRENT": Decimal(80)}, NOW)
    second = MacroSnapshot(
        {"US10Y": Decimal("4.4"), "BRENT": Decimal(88)},
        NOW + timedelta(hours=1),
    )
    assert pipeline._macro_metrics(first)["yield_change"] == 0
    changes = pipeline._macro_metrics(second)
    assert changes["yield_change"] == Decimal("0.1")
    assert changes["brent_change"] == Decimal("0.1")


def test_wilder_adx_and_live_stop_are_directionally_sane() -> None:
    bars = tuple(
        Candle(
            INSTRUMENT,
            NOW + timedelta(minutes=index),
            Decimal(100 + index),
            Decimal("100.8") + index,
            Decimal("99.8") + index,
            Decimal("100.5") + index,
            Decimal(10000),
        )
        for index in range(40)
    )
    detector = MarketRegimeDetector()
    assessment = detector.detect(bars)
    assert assessment.adx > Decimal(25)

    adjusted = detector.apply_to_plan(
        plan(),
        RegimeAssessment(
            MarketRegime.BULL_TRENDING,
            Decimal("0.001"),
            Decimal(40),
            Decimal("0.05"),
            Decimal(1),
        ),
    )
    assert adjusted.stop_loss_fraction != plan().stop_loss_fraction
    assert adjusted.take_profit_fraction == adjusted.stop_loss_fraction * adjusted.reward_risk_ratio


class AlwaysBuy:
    strategy_id = "always-buy"

    def on_bar(self, history: tuple[Candle, ...]) -> StrategySignal | None:
        return StrategySignal(Side.BUY, Decimal(1), "test") if history else None


def test_simple_replay_enters_at_next_bar_open_not_signal_close() -> None:
    bars = (
        Candle(INSTRUMENT, NOW, Decimal(100), Decimal(101), Decimal(99), Decimal(100), Decimal(1)),
        Candle(
            INSTRUMENT,
            NOW + timedelta(minutes=1),
            Decimal(200),
            Decimal(211),
            Decimal(199),
            Decimal(210),
            Decimal(1),
        ),
    )
    result = ReplayEngine(CostModel(Decimal(0), Decimal(0), Decimal(0))).run(
        bars,
        AlwaysBuy(),
        Decimal(1000),
    )
    assert result.trade_pnls == (Decimal(10),)


def test_historical_harness_decides_on_bar_n_and_executes_at_bar_n_plus_one_open(
    tmp_path, monkeypatch
) -> None:
    bars = tuple(
        Candle(
            INSTRUMENT,
            NOW + timedelta(minutes=index),
            Decimal(100 + index * 10),
            Decimal(102 + index * 10),
            Decimal(99 + index * 10),
            Decimal(101 + index * 10),
            Decimal(1000),
        )
        for index in range(3)
    )
    captured: list[tuple[datetime, Decimal]] = []

    def fake_run(self, instrument, now, capital_plan, portfolio, **kwargs):
        del self, instrument, capital_plan, portfolio
        captured.append((now, kwargs["reference_price_override"]))
        return SimpleNamespace(execution=SimpleNamespace(fill=None))

    monkeypatch.setattr(SwarmMarketAnalysisPipeline, "run", fake_run)
    broker = PaperBrokerService(tmp_path / "next-open.db", slippage_bps=Decimal(0))
    HistoricalReplayHarness(broker, plan(), quantity=1, tenant_id="replay").run(
        HistoricalReplayDataset(bars)
    )

    assert captured == [
        (bars[0].timestamp, bars[1].open),
        (bars[1].timestamp, bars[2].open),
    ]
