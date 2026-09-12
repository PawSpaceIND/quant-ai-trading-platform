import asyncio
from datetime import datetime, timezone
from decimal import Decimal

from quant_ai.agents.swarm_runtime import SwarmPaperTradingService
from quant_ai.domain.models import AssetClass, Instrument, Market, RiskMode
from quant_ai.execution.daemon import AutonomousTradingDaemon
from quant_ai.execution.notifications import TradingNotificationDispatcher
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
from quant_ai.notifications.trading import TradingAlertCode
from quant_ai.planning.capital import CapitalGoalEngine, CapitalPlanRequest


class CaptureChannel:
    def __init__(self) -> None:
        self.items = []

    def send(self, notification) -> None:
        self.items.append(notification)


def build_daemon(tmp_path):
    broker = PaperBrokerService(tmp_path / "daemon.db", starting_capital=Decimal(100000), slippage_bps=Decimal(0))
    feed = UsaSandboxMarketDataFeed()
    pipeline = SwarmMarketAnalysisPipeline(
        feed, SandboxNewsSentimentProvider(), SandboxFundamentalDataProvider(),
        SandboxMacroIndicatorProvider(), runtime=SwarmPaperTradingService(broker=broker),
    )
    scheduler = AutonomousCadenceScheduler(pipeline)
    tracker = PortfolioTracker(broker, feed, tenant_id="daemon")
    plan = CapitalGoalEngine().recommend(CapitalPlanRequest(
        Decimal(100000), Decimal("0.82"), Decimal("0.20"), expected_edge=Decimal("0.02"),
        requested_mode=RiskMode.BALANCED,
    ))
    channel = CaptureChannel()
    dispatcher = TradingNotificationDispatcher((channel,))
    instrument = Instrument("AAPL", Market.USA, AssetClass.EQUITY, "USD", "NASDAQ")
    now = datetime(2026, 9, 14, 14, tzinfo=timezone.utc)
    daemon = AutonomousTradingDaemon(
        scheduler, tracker, instrument, plan, quantity=10, country="USA", tenant_id="daemon",
        notifications=dispatcher, idle_sleep_seconds=0.01, clock=lambda: now,
    )
    return daemon, broker, channel, now


def test_run_once_dispatches_founder_brief_to_outbox(tmp_path) -> None:
    daemon, broker, channel, now = build_daemon(tmp_path)
    brief = asyncio.run(daemon.run_once(now))
    pending = daemon.notifications.pending("daemon")
    assert brief.paper_order_ids[0].startswith("PAPER-")
    assert pending[-1].code == TradingAlertCode.CADENCE_BRIEF
    assert "Drawdown" in pending[-1].message
    assert channel.items[-1] == pending[-1]
    assert len(broker.ledger_entries("daemon")) == 1


def test_daemon_stop_flushes_sqlite_and_logs_shutdown(tmp_path) -> None:
    daemon, broker, _, _ = build_daemon(tmp_path)

    async def exercise() -> None:
        task = asyncio.create_task(daemon.run())
        while not broker.ledger_entries("daemon"):
            await asyncio.sleep(0)
        daemon.request_stop()
        await task

    asyncio.run(exercise())
    broker.close()
    reopened = PaperBrokerService(tmp_path / "daemon.db", starting_capital=Decimal(100000), slippage_bps=Decimal(0))
    assert len(reopened.ledger_entries("daemon")) == 1
    assert daemon.audit.events()[-1].event_type == "daemon_shutdown"
    assert daemon.heartbeat().stopping
    reopened.close()
