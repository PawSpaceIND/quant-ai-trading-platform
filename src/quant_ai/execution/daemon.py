from __future__ import annotations

import asyncio
import logging
import signal
from dataclasses import dataclass
from datetime import datetime, timezone
from time import monotonic
from typing import Callable

from quant_ai.audit.journal import InMemoryAuditJournal
from quant_ai.domain.models import Instrument, Market
from quant_ai.execution.briefing import FounderExecutionBrief
from quant_ai.execution.notifications import TradingNotificationDispatcher
from quant_ai.execution.portfolio import PortfolioTracker
from quant_ai.execution.scheduler import AutonomousCadenceScheduler
from quant_ai.planning.capital import CapitalPlan


@dataclass(frozen=True)
class DaemonHeartbeat:
    timestamp: datetime
    uptime_seconds: float
    last_cadence_run: datetime | None
    active_market_sessions: dict[str, str]
    stopping: bool


class AutonomousTradingDaemon:
    def __init__(
        self,
        scheduler: AutonomousCadenceScheduler,
        tracker: PortfolioTracker,
        instrument: Instrument,
        plan: CapitalPlan,
        *,
        quantity: int,
        country: str,
        tenant_id: str = "default",
        notifications: TradingNotificationDispatcher | None = None,
        audit: InMemoryAuditJournal | None = None,
        idle_sleep_seconds: float = 1.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if idle_sleep_seconds <= 0:
            raise ValueError("idle sleep must be positive")
        self.scheduler = scheduler
        self.tracker = tracker
        self.instrument = instrument
        self.plan = plan
        self.quantity = quantity
        self.country = country
        self.tenant_id = tenant_id
        self.notifications = notifications or TradingNotificationDispatcher()
        self.audit = audit or InMemoryAuditJournal()
        self.idle_sleep_seconds = idle_sleep_seconds
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._started_monotonic = monotonic()
        self._stop_requested = False
        self._in_flight = False
        self._logger = logging.getLogger("quant_ai.daemon")

    def request_stop(self) -> None:
        self._stop_requested = True

    def install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.request_stop)
            except (NotImplementedError, RuntimeError):
                pass

    async def run_once(self, now: datetime | None = None) -> FounderExecutionBrief:
        timestamp = now or self.clock()
        self._in_flight = True
        try:
            pre_metrics = self.tracker.metrics(timestamp)
            before = self.tracker.get_snapshot(timestamp)
            if self.scheduler.pipeline.runtime.cio.atlas.llm_client is not None:
                brief = await self.scheduler.run_tick_async(
                    self.instrument,
                    timestamp,
                    self.plan,
                    before,
                    quantity=self.quantity,
                    country=self.country,
                    tenant_id=self.tenant_id,
                )
            else:
                brief = self.scheduler.run_tick(
                    self.instrument,
                    timestamp,
                    self.plan,
                    before,
                    quantity=self.quantity,
                    country=self.country,
                    tenant_id=self.tenant_id,
                )
            metrics = self.tracker.metrics(timestamp)
            realized_delta = metrics.realized_pnl - pre_metrics.realized_pnl
            if realized_delta != 0:
                traces = self.scheduler.pipeline.runtime.xai_logger.traces()
                if traces:
                    agent_ids = tuple(row["agent_id"] for row in traces[-1].input_matrix)
                    self.scheduler.pipeline.runtime.attribution.record(agent_ids, realized_delta)
            self.notifications.dispatch_brief(
                brief,
                total_equity=metrics.total_equity,
                realized_pnl=metrics.realized_pnl,
                unrealized_pnl=metrics.unrealized_pnl,
                drawdown_fraction=metrics.drawdown_fraction,
                tenant_id=self.tenant_id,
            )
            self.audit.append(
                "cadence_complete",
                {
                    "market_state": brief.market_state.value,
                    "mode": brief.mode,
                    "paper_orders": brief.paper_order_ids,
                    "equity": str(metrics.total_equity),
                    "drawdown": str(metrics.drawdown_fraction),
                },
            )
            return brief
        finally:
            self._in_flight = False

    async def run(self) -> None:
        self.install_signal_handlers()
        self.audit.append("daemon_started", {"tenant_id": self.tenant_id})
        try:
            while not self._stop_requested:
                now = self.clock()
                if self.scheduler.is_due(now):
                    await self.run_once(now)
                if not self._stop_requested:
                    await asyncio.sleep(self.idle_sleep_seconds)
        finally:
            self.tracker.broker.flush()
            self.audit.append(
                "daemon_shutdown",
                {"tenant_id": self.tenant_id, "in_flight": self._in_flight},
            )
            self._logger.info("autonomous trading daemon shutdown cleanly")

    def heartbeat(self, now: datetime | None = None) -> DaemonHeartbeat:
        timestamp = now or self.clock()
        sessions = {
            market.value: self.scheduler.calendar.state(market, timestamp).value
            for market in (Market.INDIA, Market.USA)
        }
        return DaemonHeartbeat(
            timestamp,
            max(0.0, monotonic() - self._started_monotonic),
            self.scheduler.last_run_at,
            sessions,
            self._stop_requested,
        )
