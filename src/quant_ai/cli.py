from __future__ import annotations

import argparse
import asyncio
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from quant_ai.agents.atlas import AtlasInvestmentAgent
from quant_ai.agents.swarm import AtlasCIOAgent, TradeProposal
from quant_ai.agents.swarm_runtime import SwarmPaperTradingService
from quant_ai.analytics.metrics import summarize_performance
from quant_ai.backtesting.replay import (
    HistoricalReplayDataset,
    HistoricalReplayHarness,
    load_replay_dataset,
)
from quant_ai.backtesting.tearsheet import build_tearsheet
from quant_ai.config import paths
from quant_ai.domain.models import AssetClass, Instrument, Market, RiskMode, Side
from quant_ai.execution.audit import XAITraceLogger
from quant_ai.execution.daemon import AutonomousTradingDaemon
from quant_ai.execution.notifications import (
    ConsoleNotificationAdapter,
    TradingNotificationDispatcher,
)
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.execution.portfolio import PortfolioTracker
from quant_ai.execution.risk_state import SQLiteRiskStateStore
from quant_ai.execution.scheduler import AutonomousCadenceScheduler
from quant_ai.governance.directives import FounderDirectives
from quant_ai.intelligence.pipeline import SwarmMarketAnalysisPipeline
from quant_ai.intelligence.sandbox import (
    SandboxFundamentalDataProvider,
    SandboxMacroIndicatorProvider,
    SandboxNewsSentimentProvider,
)
from quant_ai.marketdata.feed import UsaSandboxMarketDataFeed
from quant_ai.operations.zerodha_login import run_login
from quant_ai.planning.capital import CapitalGoalEngine, CapitalPlanRequest
from quant_ai.risk.warden import RiskWarden


def build_runtime() -> AutonomousTradingDaemon:
    database = paths.ledger_path("QUANT_AI_PAPER_DB")
    tenant_id = paths.tenant_id("QUANT_AI_TENANT_ID")
    database.parent.mkdir(parents=True, exist_ok=True)
    directives = FounderDirectives.from_env() or FounderDirectives()
    broker = PaperBrokerService(str(database), starting_capital=directives.starting_capital)
    feed = UsaSandboxMarketDataFeed()
    xai_dir = str(paths.proof_directory("PRAMANA_XAI_DIR", "QUANT_AI_XAI_DIR"))
    runtime = SwarmPaperTradingService(
        cio=AtlasCIOAgent(AtlasInvestmentAgent(founder_instructions=directives.instructions)),
        warden=RiskWarden(blocked_asset_classes=directives.blocked_asset_classes()),
        broker=broker,
        xai_logger=XAITraceLogger(xai_dir),
        max_open_positions=directives.max_open_positions,
    )
    pipeline = SwarmMarketAnalysisPipeline(
        feed,
        SandboxNewsSentimentProvider(),
        SandboxFundamentalDataProvider(),
        SandboxMacroIndicatorProvider(),
        runtime=runtime,
    )
    scheduler = AutonomousCadenceScheduler(pipeline)
    tracker = PortfolioTracker(broker, feed, tenant_id=tenant_id)
    # The sandbox runtime is US-only; the watchlist applies to the ghost daemon.
    plan = CapitalGoalEngine().recommend(directives.capital_plan_request())
    instrument = Instrument("AAPL", Market.USA, AssetClass.EQUITY, "USD", "NASDAQ")
    notifications = TradingNotificationDispatcher((ConsoleNotificationAdapter(),))
    return AutonomousTradingDaemon(
        scheduler,
        tracker,
        instrument,
        plan,
        # quantity is intentionally unset: each entry is sized from live equity and the
        # capital plan (PositionSizer.quantity_from_plan), not a fixed share count.
        country="USA",
        tenant_id=tenant_id,
        notifications=notifications,
    )


def _portfolio(daemon: AutonomousTradingDaemon) -> None:
    metrics = daemon.tracker.metrics(datetime.now(timezone.utc))
    print(f"cash={metrics.cash_balance}")
    for item in metrics.positions:
        print(
            f"{item.symbol} qty={item.quantity} avg={item.average_entry_price} "
            f"current={item.current_price} unrealized={item.unrealized_pnl}"
        )
    print(f"realized_pnl={metrics.realized_pnl}")
    print(f"unrealized_pnl={metrics.unrealized_pnl}")
    print(f"equity={metrics.total_equity}")
    print(f"drawdown={metrics.drawdown_fraction}")


def _analytics(daemon: AutonomousTradingDaemon) -> None:
    now = datetime.now(timezone.utc)
    candles = daemon.scheduler.pipeline.market_feed.fetch_ohlcv(
        daemon.instrument, now - timedelta(minutes=60), now, "1m"
    )
    closes = tuple(item.close for item in candles)
    returns = tuple(
        (after - before) / before
        for before, after in zip(closes, closes[1:])
        if before > 0
    )
    curve = [Decimal(100)]
    for item in returns:
        curve.append(curve[-1] * (Decimal(1) + item))
    metrics = summarize_performance(returns, tuple(curve), returns, returns)
    print(f"sharpe={metrics.sharpe}")
    print(f"sortino={metrics.sortino}")
    print(f"max_drawdown={metrics.max_drawdown}")
    print(f"win_loss_ratio={metrics.win_loss_ratio}")
    print(f"var_95={metrics.var_95}")
    print(f"alpha={metrics.alpha}")
    print(f"beta={metrics.beta}")
    attribution = daemon.scheduler.pipeline.runtime.attribution.attribution()
    if not attribution:
        print("agent_attribution=none")
    for row in attribution:
        print(
            f"agent={row.agent_id} hit_rate={row.hit_rate} pnl={row.pnl} "
            f"weight={row.conviction_weight}"
        )


def _stress_test(daemon: AutonomousTradingDaemon) -> None:
    metrics = daemon.tracker.metrics(datetime.now(timezone.utc))
    if not metrics.positions:
        print("stress_test=no_open_positions")
        return
    snapshot = daemon.tracker.get_snapshot()
    stress_agent = daemon.scheduler.pipeline.runtime.stress_agent
    for item in metrics.positions:
        proposal = TradeProposal(
            "cli-stress", item.symbol, item.market, daemon.country, item.asset_class,
            Side.BUY, item.quantity, item.current_price, None, None, Decimal(1), Decimal(0), Decimal(0),
            ("current_paper_position_stress_test",),
        )
        verdict = stress_agent.evaluate(proposal, snapshot)
        print(
            f"symbol={item.symbol} passed={verdict.passed} "
            f"scenario={verdict.worst_scenario} projected_loss={verdict.projected_loss} "
            f"equity_loss_fraction={verdict.loss_fraction_of_equity}"
        )


def _friction_audit(daemon: AutonomousTradingDaemon) -> None:
    broker = daemon.tracker.broker
    totals = broker.friction_totals(daemon.tenant_id)
    statutory_codes = {"STT", "EXCHANGE", "SEBI", "GST", "STAMP", "SEC", "FINRA_TAF"}
    statutory = sum((amount for code, amount in totals.items() if code in statutory_codes), Decimal(0))
    print(f"brokerage={totals.get('BROKERAGE', Decimal(0))}")
    print(f"statutory_fees={statutory}")
    print(f"slippage={totals.get('SLIPPAGE', Decimal(0))}")
    print(f"spread={totals.get('SPREAD', Decimal(0))}")
    for code in sorted(totals):
        print(f"{code.lower()}={totals[code]}")


def _backtest(args: argparse.Namespace) -> None:
    if not args.data:
        raise SystemExit("backtest requires --data")
    market = Market.INDIA if args.market == "india" else Market.USA
    instrument = (
        Instrument("RELIANCE", market, AssetClass.EQUITY, "INR", "NSE")
        if market == Market.INDIA
        else Instrument("AAPL", market, AssetClass.EQUITY, "USD", "NASDAQ")
    )
    dataset = load_replay_dataset(args.data, instrument)
    start_date = datetime.fromisoformat(args.start).date() if args.start else None
    end_date = datetime.fromisoformat(args.end).date() if args.end else None
    bars = tuple(
        bar for bar in dataset.bars
        if (start_date is None or bar.timestamp.date() >= start_date)
        and (end_date is None or bar.timestamp.date() <= end_date)
    )
    if not bars:
        raise SystemExit("no bars in requested backtest range")
    end_time = bars[-1].timestamp
    dataset = HistoricalReplayDataset(
        bars,
        tuple(item for item in dataset.macro if item.observed_at <= end_time),
        tuple(item for item in dataset.news if item.published_at <= end_time),
        tuple(item for item in dataset.fundamentals if item.observed_at <= end_time),
        dataset.benchmark_closes,
        tuple(w for w in dataset.intrabar_windows
              if w.parent_timestamp in {b.timestamp for b in bars[1:]}),
    )
    database = os.environ.get("QUANT_AI_BACKTEST_DB", "").strip() or ":memory:"
    if database == "shared":
        database = str(paths.ledger_path())
    if database != ":memory:":
        Path(database).parent.mkdir(parents=True, exist_ok=True)
    broker = PaperBrokerService(database, starting_capital=Decimal(100000))
    plan = CapitalGoalEngine().recommend(
        CapitalPlanRequest(
            Decimal(100000), Decimal("0.80"), Decimal("0.20"),
            expected_edge=Decimal("0.02"), requested_mode=RiskMode.BALANCED,
        )
    )
    proof_dir = paths.proof_directory("PRAMANA_XAI_DIR", "QUANT_AI_XAI_DIR")
    proof_dir.mkdir(parents=True, exist_ok=True)
    tenant = paths.tenant_id("QUANT_AI_TENANT_ID")
    result = HistoricalReplayHarness(
        broker,
        plan,
        quantity=None,  # dynamic: sized per bar from equity and the capital plan
        country="India" if market == Market.INDIA else "USA",
        tenant_id=tenant,
        xai_logger=XAITraceLogger(proof_dir),
    ).run(dataset)
    tearsheet_json = build_tearsheet(result, broker, tenant_id=tenant).to_json()
    (proof_dir / "latest-backtest-tearsheet.json").write_text(tearsheet_json)
    print(tearsheet_json)
    broker.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pramana")
    parser.add_argument(
        "command",
        choices=(
            "run-once", "daemon", "portfolio", "analytics", "stress-test",
            "backtest", "friction-audit", "halt", "resume", "zerodha-login",
        ),
    )
    parser.add_argument("--data")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--market", choices=("india", "us"), default="us")
    parser.add_argument("--reason", default="operator halt")
    parser.add_argument(
        "--request-token",
        help="zerodha-login: Kite request_token or the full redirect URL; prompted if omitted",
    )
    args = parser.parse_args(argv)
    if args.command == "halt":
        target = paths.halt_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(args.reason.strip() or "operator halt", encoding="utf-8")
        print(f"halt engaged: {target}")
        return 0
    if args.command == "resume":
        target = paths.halt_file()
        if target.exists():
            target.unlink()
            print(f"halt released: {target}")
        else:
            print(f"no halt file present: {target}")
        ledger = paths.ledger_path("PRAMANA_PAPER_DB", "QUANT_AI_PAPER_DB")
        if ledger.exists():
            tenant = paths.tenant_id("QUANT_AI_TENANT_ID", default="ghost")
            risk_state = SQLiteRiskStateStore(ledger)
            risk_state.set_kill_switch(tenant, False, None)
            risk_state.close()
            print(f"persisted halt released: tenant={tenant}")
        return 0
    if args.command == "zerodha-login":
        # Daily Kite token renewal for the paper pilot; never prints or stores secrets.
        return run_login(args.request_token)
    if args.command == "backtest":
        _backtest(args)
        return 0
    daemon = build_runtime()
    if args.command == "run-once":
        brief = asyncio.run(daemon.run_once())
        print(brief.to_json())
        daemon.tracker.broker.flush()
        return 0
    if args.command == "portfolio":
        _portfolio(daemon)
        daemon.tracker.broker.flush()
        return 0
    if args.command == "analytics":
        _analytics(daemon)
        daemon.tracker.broker.flush()
        return 0
    if args.command == "stress-test":
        _stress_test(daemon)
        daemon.tracker.broker.flush()
        return 0
    if args.command == "friction-audit":
        _friction_audit(daemon)
        daemon.tracker.broker.flush()
        return 0
    asyncio.run(daemon.run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
