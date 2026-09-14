from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from time import monotonic
from typing import Callable

from quant_ai.audit.journal import InMemoryAuditJournal
from quant_ai.brokers.adapter import BrokerPosition
from quant_ai.domain.models import Instrument, Market
from quant_ai.execution.briefing import FounderExecutionBrief
from quant_ai.execution.notifications import TradingNotificationDispatcher
from quant_ai.execution.portfolio import PortfolioTracker
from quant_ai.execution.protective_exits import (
    ProtectiveExit,
    ProtectiveExitEngine,
    market_feed_mark_resolver,
)
from quant_ai.execution.scheduler import AutonomousCadenceScheduler
from quant_ai.execution.session import MarketState
from quant_ai.governance.directives import country_for
from quant_ai.notifications.trading import TradingAlertCode
from quant_ai.operations.kill_switch import KillSwitch
from quant_ai.planning.capital import CapitalPlan

OPERATOR_HALT_PREFIX = "operator_halt_file"


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
        quantity: int | None = None,
        country: str,
        tenant_id: str = "default",
        notifications: TradingNotificationDispatcher | None = None,
        audit: InMemoryAuditJournal | None = None,
        idle_sleep_seconds: float = 1.0,
        clock: Callable[[], datetime] | None = None,
        exit_engine: ProtectiveExitEngine | None = None,
        halt_file: str | Path | None = None,
        instruments: Iterable[Instrument] | None = None,
    ) -> None:
        if idle_sleep_seconds <= 0:
            raise ValueError("idle sleep must be positive")
        # The founder's watchlist. Every instrument is evaluated on every cadence tick;
        # the first one keeps the legacy single-instrument attributes working.
        self.instruments = tuple(instruments) if instruments else (instrument,)
        if not self.instruments:
            raise ValueError("at least one instrument is required")
        instrument = self.instruments[0]
        self.briefs: tuple[FounderExecutionBrief, ...] = ()
        # Operator halt: a marker file an operator can create from outside the process.
        self.halt_file = Path(halt_file) if halt_file is not None else None
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
        # C1: protective exits are checked on every tick, before any new analysis.
        self.exit_engine = exit_engine or self._default_exit_engine()
        self.protective_exits: tuple[ProtectiveExit, ...] = ()
        self._started_monotonic = monotonic()
        self._stop_requested = False
        self._in_flight = False
        self._logger = logging.getLogger("quant_ai.daemon")

    def _default_exit_engine(self) -> ProtectiveExitEngine:
        return ProtectiveExitEngine(
            self.tracker.broker,
            market_feed_mark_resolver(
                self.tracker.market_feed,
                self.tracker.instrument_resolver,
                getattr(self.scheduler.pipeline, "tick_reader", None),
                self.clock,
            ),
            tenant_id=self.tenant_id,
            dispatcher=self.notifications,
        )

    @property
    def kill_switch(self) -> KillSwitch:
        """The single halt control shared with the daemon's execution runtime."""
        return self.scheduler.pipeline.runtime.kill_switch

    def engage_kill_switch(self, reason: str) -> None:
        """C3/C5: latch a halt that survives until an operator resets it."""
        if self.kill_switch.engaged:
            return
        self.kill_switch.engage(reason)
        self.audit.append("kill_switch_engaged", {"reason": reason, "tenant_id": self.tenant_id})
        self.notifications.dispatch(
            TradingAlertCode.KILL_SWITCH_ENGAGED,
            f"Trading halted: {reason}",
            tenant_id=self.tenant_id,
            metadata={"reason": reason},
        )

    def apply_operator_halt(self) -> None:
        """Engage the kill switch while the halt file exists; release when it is removed.

        Only a halt the file engaged is released by its removal: a halt latched by
        repeated cadence failures stays until an operator restarts the daemon.
        Protective exits keep running during a halt - a halt freezes new risk, never
        the ability to cut it.
        """
        if self.halt_file is None:
            return
        reason = self.kill_switch.reason or ""
        if self.halt_file.exists():
            if not self.kill_switch.engaged:
                note = self.halt_file.read_text(encoding="utf-8").strip() or str(self.halt_file)
                self.engage_kill_switch(f"{OPERATOR_HALT_PREFIX}: {note}")
        elif self.kill_switch.engaged and reason.startswith(OPERATOR_HALT_PREFIX):
            self.kill_switch.reset()
            self.audit.append(
                "kill_switch_released",
                {"tenant_id": self.tenant_id, "via": "halt_file_removed"},
            )

    def notify_cadence_failure(self, detail: str, consecutive: int) -> None:
        self.audit.append(
            "cadence_tick_failed",
            {"detail": detail, "consecutive": str(consecutive), "tenant_id": self.tenant_id},
        )
        self.notifications.dispatch(
            TradingAlertCode.CADENCE_TICK_FAILED,
            f"Cadence tick failed ({consecutive} consecutive): {detail}",
            tenant_id=self.tenant_id,
            metadata={"detail": detail, "consecutive": str(consecutive)},
        )

    def sweep_protective_exits(
        self, now: datetime | None = None
    ) -> tuple[ProtectiveExit, ...]:
        """Mark open positions and liquidate any whose stop or target is breached."""
        exits = self.exit_engine.evaluate(now or self.clock())
        for item in exits:
            self.audit.append(
                "protective_exit",
                {
                    "symbol": item.symbol,
                    "trigger": item.trigger.value,
                    "threshold": str(item.threshold),
                    "mark_price": str(item.mark_price),
                    "quantity": str(item.quantity),
                    "filled": str(item.filled),
                    "order_id": item.order_id or "",
                },
            )
        return exits

    def country_of(self, instrument: Instrument) -> str:
        """Country charged for an instrument under the warden's allocation cap."""
        if instrument == self.instrument:
            return self.country
        return country_for(instrument)

    def country_exposure(self, now: datetime | None = None) -> dict[str, Decimal]:
        """Market value of open positions per country, so the country cap is real."""
        exposure: dict[str, Decimal] = {}
        for position in self.tracker.metrics(now).positions:
            country = self.country_of(
                self.tracker.instrument_resolver(
                    BrokerPosition(
                        self.tenant_id, position.symbol, position.market, position.asset_class,
                        position.quantity, position.average_entry_price,
                    )
                )
            )
            exposure[country] = exposure.get(country, Decimal(0)) + position.market_value
        return exposure

    @staticmethod
    def _primary_brief(briefs: list[FounderExecutionBrief]) -> FounderExecutionBrief:
        for item in briefs:
            if item.market_state == MarketState.REGULAR_HOURS:
                return item
        return briefs[0]

    @staticmethod
    def _briefs_to_dispatch(briefs: list[FounderExecutionBrief]) -> tuple[FounderExecutionBrief, ...]:
        """One brief per instrument in session; a single closed-market sweep otherwise."""
        if len(briefs) == 1:
            return tuple(briefs)
        in_session = tuple(item for item in briefs if item.market_state == MarketState.REGULAR_HOURS)
        return in_session or (briefs[0],)

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
            self.apply_operator_halt()
            # C1: liquidate breached positions BEFORE new analysis, so a stop is honoured
            # even on a tick where the swarm would otherwise want to add exposure.
            self.protective_exits = self.sweep_protective_exits(timestamp)
            pre_metrics = self.tracker.metrics(timestamp)
            use_llm = self.scheduler.pipeline.runtime.cio.atlas.llm_client is not None
            briefs: list[FounderExecutionBrief] = []
            for instrument in self.instruments:
                # A fresh snapshot per instrument: a fill earlier in the loop changes the
                # exposure every later instrument is judged against.
                before = self.tracker.get_snapshot(timestamp)
                exposure = self.country_exposure(timestamp)
                if use_llm:
                    brief = await self.scheduler.run_tick_async(
                        instrument,
                        timestamp,
                        self.plan,
                        before,
                        quantity=self.quantity,
                        country=self.country_of(instrument),
                        tenant_id=self.tenant_id,
                        country_exposure=exposure,
                    )
                else:
                    brief = self.scheduler.run_tick(
                        instrument,
                        timestamp,
                        self.plan,
                        before,
                        quantity=self.quantity,
                        country=self.country_of(instrument),
                        tenant_id=self.tenant_id,
                        country_exposure=exposure,
                    )
                briefs.append(brief)
            self.briefs = tuple(briefs)
            brief = self._primary_brief(briefs)
            metrics = self.tracker.metrics(timestamp)
            realized_delta = metrics.realized_pnl - pre_metrics.realized_pnl
            if realized_delta != 0:
                traces = self.scheduler.pipeline.runtime.xai_logger.traces()
                if traces:
                    agent_ids = tuple(row["agent_id"] for row in traces[-1].input_matrix)
                    self.scheduler.pipeline.runtime.attribution.record(agent_ids, realized_delta)
            for item in self._briefs_to_dispatch(briefs):
                self.notifications.dispatch_brief(
                    item,
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
                    "subjects": tuple(item.subject for item in briefs),
                    "paper_orders": tuple(oid for item in briefs for oid in item.paper_order_ids),
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
