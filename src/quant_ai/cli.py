from __future__ import annotations

import argparse
import asyncio
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from quant_ai.agents.swarm import TradeProposal
from quant_ai.agents.swarm_runtime import SwarmPaperTradingService
from quant_ai.analytics.metrics import summarize_performance
from quant_ai.domain.models import AssetClass, Instrument, Market, RiskMode, Side
from quant_ai.execution.audit import XAITraceLogger
from quant_ai.execution.daemon import AutonomousTradingDaemon
from quant_ai.execution.notifications import (
    ConsoleNotificationAdapter,
    TradingNotificationDispatcher,
)
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


def build_runtime() -> AutonomousTradingDaemon:
    database = os.environ.get("QUANT_AI_PAPER_DB", "quant-ai-paper.db")
    tenant_id = os.environ.get("QUANT_AI_TENANT_ID", "default")
    broker = PaperBrokerService(database, starting_capital=Decimal(100000))
    feed = UsaSandboxMarketDataFeed()
    xai_dir = os.environ.get("QUANT_AI_XAI_DIR", "quant-ai-xai")
    runtime = SwarmPaperTradingService(broker=broker, xai_logger=XAITraceLogger(xai_dir))
    pipeline = SwarmMarketAnalysisPipeline(
        feed,
        SandboxNewsSentimentProvider(),
        SandboxFundamentalDataProvider(),
        SandboxMacroIndicatorProvider(),
        runtime=runtime,
    )
    scheduler = AutonomousCadenceScheduler(pipeline)
    tracker = PortfolioTracker(broker, feed, tenant_id=tenant_id)
    plan = CapitalGoalEngine().recommend(
        CapitalPlanRequest(
            Decimal(100000),
            Decimal("0.80"),
            Decimal("0.20"),
            expected_edge=Decimal("0.02"),
            requested_mode=RiskMode.BALANCED,
        )
    )
    instrument = Instrument("AAPL", Market.USA, AssetClass.EQUITY, "USD", "NASDAQ")
    notifications = TradingNotificationDispatcher((ConsoleNotificationAdapter(),))
    return AutonomousTradingDaemon(
        scheduler,
        tracker,
        instrument,
        plan,
        quantity=10,
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="quant-ai")
    parser.add_argument(
        "command", choices=("run-once", "daemon", "portfolio", "analytics", "stress-test")
    )
    args = parser.parse_args(argv)
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
    asyncio.run(daemon.run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
