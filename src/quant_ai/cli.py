from __future__ import annotations

import argparse
import asyncio
import os
from datetime import datetime, timezone
from decimal import Decimal

from quant_ai.agents.swarm_runtime import SwarmPaperTradingService
from quant_ai.domain.models import AssetClass, Instrument, Market, RiskMode
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
    runtime = SwarmPaperTradingService(broker=broker)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="quant-ai")
    parser.add_argument("command", choices=("run-once", "daemon", "portfolio"))
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
    asyncio.run(daemon.run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
