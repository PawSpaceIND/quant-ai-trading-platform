from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, DecimalException
from pathlib import Path
from time import monotonic
from typing import Callable
from zoneinfo import ZoneInfo

from quant_ai.audit.journal import InMemoryAuditJournal
from quant_ai.brokers.adapter import BrokerPosition
from quant_ai.domain.models import Instrument, Market, Side
from quant_ai.execution.briefing import FounderExecutionBrief
from quant_ai.execution.ledger_integrity import PaperLedgerDataError
from quant_ai.execution.notifications import TradingNotificationDispatcher
from quant_ai.execution.overnight import DeclaredActionLookup, OvernightGapMonitor
from quant_ai.execution.portfolio import PortfolioTracker
from quant_ai.execution.protection_state import positive_level
from quant_ai.execution.protective_exits import (
    ProtectiveExit,
    ProtectiveExitEngine,
    market_feed_mark_resolver,
)
from quant_ai.execution.scheduler import AutonomousCadenceScheduler
from quant_ai.execution.session import MarketState
from quant_ai.governance.directives import country_for
from quant_ai.governance.event_calendar import EventCalendar
from quant_ai.marketdata.corporate_calendar import CorporateActionCalendar
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
        event_calendar: EventCalendar | None = None,
        declared_action_lookup: DeclaredActionLookup | None = None,
        missed_opportunity_dir: str | Path | None = None,
        post_mortem_dir: str | Path | None = None,
        specialist_reweighting: bool = True,
    ) -> None:
        if idle_sleep_seconds <= 0:
            raise ValueError("idle sleep must be positive")
        self.instruments = tuple(instruments) if instruments else (instrument,)
        if not self.instruments:
            raise ValueError("at least one instrument is required")
        instrument = self.instruments[0]
        self.briefs: tuple[FounderExecutionBrief, ...] = ()
        self.halt_file = Path(halt_file) if halt_file is not None else None
        # Operator-supplied scheduled events (earnings, policy decisions, budget day).
        # None means no blackouts. It suppresses new entries only; exits never read it.
        self.event_calendar = event_calendar
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
        # Declared ex-dates, when the operator keeps any: the only evidence that resolves
        # a price discontinuity outright. A callable rather than a calendar type so an
        # operator's own source attaches without the daemon owning a file format. None
        # means every large step across a session boundary stays an open question.
        self.declared_action_lookup = declared_action_lookup
        self.exit_engine = exit_engine or self._default_exit_engine()
        self.protective_exits: tuple[ProtectiveExit, ...] = ()
        self._started_monotonic = monotonic()
        self._stop_requested = False
        self._in_flight = False
        self._logger = logging.getLogger("quant_ai.daemon")
        self.telemetry = None
        # A protected position that cannot be priced is unprotected in fact. Hold the halt
        # for a short grace period so a single dropped tick does not stop the day, but never
        # longer than the market can move against an unenforced stop.
        self.unprotected_halt_seconds = float(
            os.getenv("PRAMANA_UNPROTECTED_HALT_SECONDS", "120") or 120
        )
        self._unprotected_since: dict[str, datetime] = {}
        raw_flatten = os.getenv("PRAMANA_SESSION_FLATTEN_MINUTES", "0").strip() or "0"
        try:
            self.session_flatten_minutes = int(raw_flatten)
        except ValueError as error:
            raise ValueError("invalid_session_flatten_minutes") from error
        if not 0 <= self.session_flatten_minutes <= 120:
            raise ValueError("invalid_session_flatten_minutes")
        self.reconciliation = None
        self.protection_coverage = None
        self.trade_evidence = None
        self.strategy_manifest = None
        # Decision-quality layer: the journal lives in the ledger; the report is a file the
        # factory points at (None keeps the report unwritten). Neither may break a tick.
        self.decision_quality_report_path: Path | None = None
        self.decision_outcomes: dict[str, int] | None = None
        # Missed-opportunity files, one per IST session date under this directory, and the
        # end-of-session note that reads them out. None keeps both off. On 21 September
        # 2026, the first twelve-name session, every decision was a hold, and nothing in
        # the evidence said what the holds had let go by.
        self.missed_opportunity_dir = (
            Path(missed_opportunity_dir) if missed_opportunity_dir is not None else None
        )
        self._missed_notified: set[str] = set()
        # Governed learning. The post-mortem for a session is built once, after the close
        # and an hour after its last decision, and written pending: its lessons reach a
        # decision only after `pramana post-mortem --approve`. The specialist skill weights
        # are recomputed once per IST week from the scored journal and applied inside a
        # fixed band when the operator's switch is on; the report is written either way.
        self.post_mortem_dir = Path(post_mortem_dir) if post_mortem_dir is not None else None
        self.specialist_reweighting = bool(specialist_reweighting)
        self._post_mortems_built: set[str] = set()
        self._skill_week: str | None = None

        # Fault halts share the portfolio's durable risk-state backend. A process or host
        # restart therefore cannot silently clear a breaker that was tripped by the runner.
        engaged, reason = self.tracker.risk_state.kill_switch_state(self.tenant_id)
        if engaged:
            self.kill_switch.engage(reason or "persisted risk halt")

    def enable_pilot_monitoring(self) -> None:
        from quant_ai.execution.telemetry import PilotTelemetry
        self.telemetry = PilotTelemetry(self)
        runtime = self.scheduler.pipeline.runtime
        runtime.snapshot_provider = lambda: self.tracker.get_snapshot(self.clock())
        runtime.pre_submit_check = self._pilot_pre_submit
        from quant_ai.agents.institutional_runtime import InstitutionalSwarmPaperTradingService
        if isinstance(runtime, InstitutionalSwarmPaperTradingService):
            runtime.bind_inflight_preflight(self._pilot_inflight_pre_submit)
        self.tracker.broker.get_starting_capital(self.tenant_id)
        self.check_protection_coverage(self.clock())
        self._reconcile_pilot()

    def check_protection_coverage(self, now: datetime) -> bool:
        self.protection_coverage = self.tracker.broker.protection_coverage(self.tenant_id, now)
        complete = self.protection_coverage["status"] == "complete"
        if not complete:
            self.engage_kill_switch("paper_position_protection_incomplete")
        return complete

    def _reconcile_pilot(self) -> bool:
        self.reconciliation = self.tracker.broker.reconcile(self.tenant_id)
        if self.reconciliation["status"] != "matched":
            self.trade_evidence = None
            self.engage_kill_switch("paper_ledger_reconciliation_failed")
            return False
        from quant_ai.validation.trade_evidence import build_trade_evidence
        with self.tracker.broker._lock:
            self.trade_evidence = build_trade_evidence(self.tracker.broker._connection, self.tenant_id)
        return True

    def _pilot_inflight_pre_submit(self, proposal, in_flight) -> str | None:
        """Recheck all operating gates while recognizing only this claimed child."""
        return self._pilot_pre_submit(proposal, in_flight=in_flight)

    def _pilot_pre_submit(self, proposal, *, in_flight=None) -> str | None:
        self.apply_operator_halt()
        if proposal.side != Side.SELL and not self.check_protection_coverage(self.clock()):
            return "pilot_protection_incomplete"
        if self.strategy_manifest is not None:
            manifest = self.strategy_manifest.check(self.clock(), force_source=True)
            if manifest['status'] in {'changed', 'unavailable'} and proposal.side != Side.SELL:
                self.engage_kill_switch('runtime_strategy_changed_or_unavailable')
                return 'pilot_strategy_manifest_unverified'
        if proposal.side != Side.SELL:
            from quant_ai.governance.runtime_identity import runtime_identity_entry_issue
            issue = runtime_identity_entry_issue(self, in_flight=in_flight)
            if issue is not None:
                self.engage_kill_switch(issue)
                return issue
        if proposal.side != Side.SELL and not self._reconcile_pilot():
            return "pilot_reconciliation_failed"
        if self.kill_switch.engaged and proposal.side != Side.SELL:
            return "pilot_halted"
        now = self.clock()
        if proposal.side != Side.SELL and self.event_calendar is not None:
            # A scheduled event suppresses a new entry and nothing else: an open position
            # keeps its stops, and a SELL is never blocked by a blackout.
            blackout = self.event_calendar.blackout_reason(proposal.symbol, now)
            if blackout is not None:
                return blackout
        instrument = next((i for i in self.instruments if i.symbol == proposal.symbol), None)
        if instrument is None or self.scheduler.calendar.state(
            instrument.market, now, exchange=instrument.exchange
        ) != MarketState.REGULAR_HOURS:
            return "pilot_session_or_scope_blocked"
        if not self.telemetry.fresh(instrument, now)[0]:
            return "pilot_stale_entry_price"
        mark = self.tracker.market_feed.latest_tick(instrument).last_price
        if proposal.reference_price <= 0 or abs(mark / proposal.reference_price - 1) > Decimal(".002"):
            return "pilot_price_moved_during_analysis"
        for position in self.tracker.broker.get_positions(self.tenant_id):
            resolved = self.tracker.instrument_resolver(position)
            if not self.telemetry.fresh(resolved, now)[0]:
                return "pilot_stale_portfolio_mark"
        return None

    def protection_tick(self, now: datetime | None = None) -> None:
        timestamp = now or self.clock()
        with self.tracker.broker._lock:
            self.apply_operator_halt()
            if self.telemetry is not None:
                self.check_protection_coverage(timestamp)
            try:
                exits = list(self.sweep_protective_exits(timestamp))
                exits.extend(self._flatten_session_positions(timestamp))
                self.protective_exits = tuple(exits)
            except Exception:
                # Durably halt before the runner reports/retries an unexpected failure.
                self.engage_kill_switch("protective_exit_failed")
                raise
            if any(not exit.filled for exit in self.protective_exits):
                self.engage_kill_switch("protective_exit_failed")
            self._check_protection_reachable(timestamp)
            self._check_overnight_gap(timestamp)
            if self.telemetry is not None:
                if any(exit.filled for exit in self.protective_exits):
                    self._reconcile_pilot()
                if self.strategy_manifest is not None:
                    manifest = self.strategy_manifest.check(timestamp)
                    if manifest['status'] in {'changed', 'unavailable'}:
                        self.engage_kill_switch('runtime_strategy_changed_or_unavailable')
                try:
                    metrics = self.tracker.metrics(timestamp)
                except (ValueError, DecimalException, OverflowError) as error:
                    reason = str(error) if isinstance(error, PaperLedgerDataError) else "invalid_account_or_valuation"
                    self.telemetry.publish_unavailable(timestamp, reason)
                    return
                daily_limit = min(self.plan.max_daily_loss_fraction, Decimal(".02"))
                opening = metrics.total_equity - metrics.daily_total_pnl
                if metrics.drawdown_fraction >= min(self.plan.max_drawdown_fraction, Decimal(".10")):
                    self.engage_kill_switch("portfolio_drawdown_limit")
                elif opening > 0 and -metrics.daily_total_pnl / opening >= daily_limit:
                    self.engage_kill_switch("portfolio_daily_loss_limit")
                self.telemetry.publish(timestamp)

    def _flatten_session_positions(self, now: datetime) -> tuple[ProtectiveExit, ...]:
        """Flatten positions inside the configured pre-close window.

        Disabled by default. The India paper launcher arms it explicitly so a demo day
        ends flat. The check is exchange-specific and runs on the independent protection
        heartbeat, so it does not wait for an AI cadence tick.
        """
        if self.session_flatten_minutes <= 0:
            return ()
        due: set[str] = set()
        for instrument in self.instruments:
            if self.scheduler.calendar.state(
                instrument.market, now, exchange=instrument.exchange
            ) != MarketState.REGULAR_HOURS:
                continue
            session = self.scheduler.calendar.session(
                instrument.market, exchange=instrument.exchange
            )
            zone = ZoneInfo(session.timezone)
            local = now.astimezone(zone)
            regular_close, _ = session.closes_on(local.date())
            close_at = datetime.combine(local.date(), regular_close, tzinfo=zone)
            remaining = close_at - local
            if timedelta(0) <= remaining <= timedelta(minutes=self.session_flatten_minutes):
                due.add(instrument.symbol)
        if not due:
            return ()
        return self.exit_engine.flatten_session(now, symbols=due)

    def bind_strategy_manifest(self, streams=(), **options) -> None:
        from quant_ai.governance.runtime_manifest import RuntimeManifest
        self.strategy_manifest = RuntimeManifest(self, streams, **options)
        self.strategy_manifest.check(self.clock())
        self.scheduler.pipeline.runtime.strategy_manifest_provider = lambda: self.strategy_manifest.summary
        self.exit_engine.strategy_manifest_provider = lambda now: self.strategy_manifest.check(now, force_source=True)
        if self.telemetry is not None:
            self._reconcile_pilot()

    def _default_exit_engine(self) -> ProtectiveExitEngine:
        # One calendar for both readers: the guard that suspends a stop on a declared
        # ex-date, and the monitor that decides whether a suspension is explained.
        calendar = CorporateActionCalendar.from_file(
            os.getenv("PRAMANA_CORPORATE_ACTIONS") or None
        )
        return ProtectiveExitEngine(
            self.tracker.broker,
            market_feed_mark_resolver(
                self.tracker.market_feed,
                self.tracker.instrument_resolver,
                getattr(self.scheduler.pipeline, "tick_reader", None),
                lambda: self.clock(),
            ),
            tenant_id=self.tenant_id,
            dispatcher=self.notifications,
            corporate_calendar=calendar,
            gap_monitor=self._env_gap_monitor(calendar),
        )

    def _env_gap_monitor(self, calendar: CorporateActionCalendar) -> OvernightGapMonitor | None:
        """The overnight gap monitor, when the operator armed it.

        Off unless ``PRAMANA_OVERNIGHT_GAP_MONITOR=session``. Arming it is a deliberate
        act because it ends in a halt: a discontinuity nobody explains within a full
        trading session stops new risk. Unarmed, the daemon behaves exactly as it did
        before - the step guard still suspends a re-based quote, it just never says so and
        never stops doing it.

        It reads the same declared ex-dates the suspension guard reads, so the two can
        never disagree about whether today's quote was re-based on purpose. An explicitly
        supplied lookup wins, for an operator whose ex-dates live somewhere else.
        """
        source = os.getenv("PRAMANA_OVERNIGHT_GAP_MONITOR", "none").strip().lower()
        if source in {"", "none"}:
            return None
        if source != "session":
            raise RuntimeError(f"unsupported PRAMANA_OVERNIGHT_GAP_MONITOR: {source}")
        return OvernightGapMonitor(
            dispatcher=self.notifications,
            tenant_id=self.tenant_id,
            declared_action_lookup=self.declared_action_lookup or calendar.action_on,
        )

    @property
    def kill_switch(self) -> KillSwitch:
        """The single halt control shared with the daemon's execution runtime."""
        return self.scheduler.pipeline.runtime.kill_switch

    def engage_kill_switch(self, reason: str) -> None:
        """Latch and durably persist a halt until an operator resets it."""
        if self.kill_switch.engaged:
            return
        self.kill_switch.engage(reason)
        self.tracker.risk_state.set_kill_switch(self.tenant_id, True, reason)
        self.audit.append(
            "kill_switch_engaged", {"reason": reason, "tenant_id": self.tenant_id}
        )
        self.notifications.dispatch(
            TradingAlertCode.KILL_SWITCH_ENGAGED,
            f"Trading halted: {reason}",
            tenant_id=self.tenant_id,
            metadata={"reason": reason},
        )

    def reset_kill_switch(self, *, via: str = "operator_reset") -> None:
        """Reset both the shared in-memory switch and its durable representation."""
        self.tracker.risk_state.set_kill_switch(self.tenant_id, False, None)
        self.kill_switch.reset()
        self.audit.append(
            "kill_switch_released",
            {"tenant_id": self.tenant_id, "via": via},
        )

    def apply_operator_halt(self) -> None:
        """Engage while the halt file exists; release only file-originated halts on removal.

        A fault halt is durable and is never cleared merely by a process restart or by the
        absence of the operator halt marker. Protective exits keep running during every halt.
        """
        if self.halt_file is None:
            return
        reason = self.kill_switch.reason or ""
        if self.halt_file.exists():
            if not self.kill_switch.engaged:
                note = self.halt_file.read_text(encoding="utf-8").strip() or str(self.halt_file)
                self.engage_kill_switch(f"{OPERATOR_HALT_PREFIX}: {note}")
        elif self.kill_switch.engaged and reason.startswith(OPERATOR_HALT_PREFIX):
            self.reset_kill_switch(via="halt_file_removed")

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

    def _check_protection_reachable(self, now: datetime) -> None:
        """Halt when an open position has been unpriceable for too long.

        Skipping an unknown mark keeps a single sweep safe, but a stop that cannot be
        evaluated is not enforced. Without this, a dead websocket leaves every position
        naked while the cadence still completes and the container still reports healthy,
        so nothing tells the operator the book lost its protection.

        The halt blocks new risk rather than liquidating: exiting on a feed we cannot
        price is exactly the fabricated-mark behaviour the exit engine refuses to do.
        Recovery is deliberately an operator decision, because an automatic resume would
        re-arm trading on a feed nobody has confirmed is healthy.
        """
        unprotected = set(getattr(self.exit_engine, "unprotected", ()))
        for symbol in list(self._unprotected_since):
            if symbol not in unprotected:
                del self._unprotected_since[symbol]
        if not unprotected:
            return
        for symbol in unprotected:
            first_seen = self._unprotected_since.setdefault(symbol, now)
            if (now - first_seen).total_seconds() >= self.unprotected_halt_seconds:
                self.engage_kill_switch(f"protection_unreachable:{symbol}")
                return

    @property
    def unprotected_since(self) -> dict[str, datetime]:
        """When each currently-unpriceable symbol first went unpriced, by symbol.

        The same clock ``_check_protection_reachable`` halts on, exposed read-only so a
        surface can say how long a stop has been unenforceable rather than only that the
        halt has already fired. A copy, because nothing outside that check may move the
        moment a halt is measured from.
        """
        return dict(self._unprotected_since)

    def _check_overnight_gap(self, now: datetime) -> None:
        """Halt when a price discontinuity has gone a full session without an explanation.

        A step across a session boundary that is larger than the exchange band is either a
        corporate action, which makes the stored stop and cost basis refer to a different
        unit, or a catastrophic gap, which makes them refer to a position that has already
        lost most of its value. The engine cannot tell those apart, and the monitor has
        been saying so on a bounded cadence since the step appeared. Once a whole trading
        session has passed without an operator resolving it, the book is being run on
        numbers nobody has stood behind, and that is where new risk stops.

        The halt blocks entries rather than liquidating, for the same reason the
        unpriceable-mark halt does: acting on a quote the engine has just admitted it
        cannot interpret is how the ambiguity turns into a booked loss. Protective exits
        keep running throughout, against the levels they always used.
        """
        monitor = getattr(self.exit_engine, "gap_monitor", None)
        if monitor is None:
            return
        reason = monitor.halt_reason(now)
        if reason is not None:
            self.engage_kill_switch(reason)

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
        key = (instrument.symbol, instrument.market, instrument.asset_class)
        for index, configured in enumerate(self.instruments):
            configured_key = (configured.symbol, configured.market, configured.asset_class)
            if configured_key == key:
                return self.country if index == 0 else country_for(configured)
        return country_for(instrument)

    def country_exposure(self, now: datetime | None = None) -> dict[str, Decimal]:
        """Market value of open positions per country, so the country cap is real."""
        exposure: dict[str, Decimal] = {}
        for position in self.tracker.metrics(now).positions:
            country = self.country_of(
                self.tracker.instrument_resolver(
                    BrokerPosition(
                        self.tenant_id,
                        position.symbol,
                        position.market,
                        position.asset_class,
                        position.quantity,
                        position.average_entry_price,
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
    def _briefs_to_dispatch(
        briefs: list[FounderExecutionBrief],
    ) -> tuple[FounderExecutionBrief, ...]:
        """The in-session briefs, or the single closed-market sweep.

        Several in-session briefs go out as one cycle digest, never one message each:
        twelve digests in one burst drew Telegram's rate limit on 21 September 2026.
        """
        if len(briefs) == 1:
            return tuple(briefs)
        in_session = tuple(
            item for item in briefs if item.market_state == MarketState.REGULAR_HOURS
        )
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
            with self.tracker.broker._lock:
                self.apply_operator_halt()
                self.protective_exits = self.sweep_protective_exits(timestamp)
            pre_metrics = self.tracker.metrics(timestamp)
            use_llm = self.scheduler.pipeline.runtime.cio.atlas.llm_client is not None
            briefs: list[FounderExecutionBrief] = []
            for instrument in self.instruments:
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
                self._journal_decision(instrument, timestamp, llm_available=use_llm)
            if self.telemetry is not None:
                self._reconcile_pilot()
            self._resolve_decision_outcomes(timestamp)
            self.briefs = tuple(briefs)
            brief = self._primary_brief(briefs)
            metrics = self.tracker.metrics(timestamp)
            realized_delta = metrics.realized_pnl - pre_metrics.realized_pnl
            if realized_delta != 0:
                traces = self.scheduler.pipeline.runtime.xai_logger.traces()
                if traces:
                    agent_ids = tuple(row["agent_id"] for row in traces[-1].input_matrix)
                    # Bound live attribution ignores these latest-trace IDs and delta.
                    # Its callback refreshes exact resolved entry evidence instead;
                    # unbound offline engines retain the explicit record API.
                    self.scheduler.pipeline.runtime.attribution.record(
                        agent_ids, realized_delta, getattr(traces[-1], "regime", None)
                    )
            to_dispatch = self._briefs_to_dispatch(briefs)
            if len(to_dispatch) == 1:
                self.notifications.dispatch_brief(
                    to_dispatch[0],
                    total_equity=metrics.total_equity,
                    realized_pnl=metrics.realized_pnl,
                    unrealized_pnl=metrics.unrealized_pnl,
                    drawdown_fraction=metrics.drawdown_fraction,
                    tenant_id=self.tenant_id,
                )
            else:
                self.notifications.dispatch_cycle_digest(
                    to_dispatch,
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
                    "paper_orders": tuple(
                        oid for item in briefs for oid in item.paper_order_ids
                    ),
                    "equity": str(metrics.total_equity),
                    "drawdown": str(metrics.drawdown_fraction),
                },
            )
            self._write_decision_quality(timestamp)
            self._write_missed_opportunities(timestamp)
            self._write_post_mortem(timestamp)
            return brief
        finally:
            self._in_flight = False

    # ------------------------------------------------------------ decision quality
    # Every hook below swallows its own failure: the journal, the resolver and the report
    # are evidence about the cadence, never a reason for the cadence to fail.

    def _journal_decision(self, instrument: Instrument, timestamp: datetime, *, llm_available: bool) -> None:
        """Journal the swarm decision the scheduler just produced for ``instrument``."""
        try:
            result = getattr(self.scheduler, "last_result", None)
            execution = getattr(result, "execution", None)
            if execution is None or execution.proposal.symbol != instrument.symbol:
                return  # off-hours sweep, or no swarm decision for this instrument
            from quant_ai.analytics.decision_journal import record_decision

            # Two regime vocabularies exist. ``result.regime`` is the sizing detector that
            # scales gross exposure; the journal instead records the multi-timeframe label
            # the decision was actually made under, which is the one the supplied evidence
            # carried and the one the proof stores, so a by-regime breakdown and the proof
            # a founder opens from it always say the same word. ``record_decision`` reads it
            # from the trace, falling back to the proposal provenance.
            record_decision(
                self.tracker.broker, execution, tenant_id=self.tenant_id,
                now=timestamp, llm_available=llm_available,
                features=getattr(result, "features", None),
            )
        except Exception:  # evidence capture must never break the cadence
            self._logger.exception("decision_journal_write_failed symbol=%s", instrument.symbol)

    def _resolve_decision_outcomes(self, timestamp: datetime) -> None:
        """Mark forward returns and closed-trade outcomes for earlier journal rows."""
        try:
            from quant_ai.analytics.outcome_resolver import resolve_outcomes

            self.decision_outcomes = resolve_outcomes(
                self.tracker.broker, tenant_id=self.tenant_id, now=timestamp,
                mark_for=self._decision_mark, calendar=self.scheduler.calendar,
                market=self.instrument.market, trade_evidence=self.trade_evidence,
            )
        except Exception:  # see above: evidence never breaks the cadence
            self._logger.exception("decision_outcome_resolution_failed")

    def _write_decision_quality(self, timestamp: datetime) -> None:
        """Rewrite the decision-quality report file, when the factory configured one."""
        if self.decision_quality_report_path is None:
            return
        try:
            from quant_ai.analytics.decision_quality import build_report, write_report

            report = build_report(self.tracker.broker, tenant_id=self.tenant_id, now=timestamp)
            from quant_ai.analytics.learning_monitor import enrich_learning_report

            observed_at = self.clock()
            self.scheduler.pipeline.runtime.attribution._refresh_bound(observed_at)
            try:
                skill = self._refresh_specialist_skill(timestamp)
            except Exception:  # the skill report is optional evidence; the quality report still writes
                self._logger.exception("specialist_skill_refresh_failed")
                skill = None
            if skill is not None:
                from quant_ai.analytics.specialist_skill import summary as skill_summary

                report["specialist_skill"] = {**skill_summary(skill), "applied_to_engine": self.specialist_reweighting}
            drift = enrich_learning_report(
                report, self.scheduler.pipeline.runtime.attribution,
                config_path=self.decision_quality_report_path.parent / "learning-monitor.json",
                tenant_id=self.tenant_id, now=observed_at,
            )
            budget = self._ai_budget_status()
            if budget is not None:
                report["ai_budget"] = budget
            write_report(self.decision_quality_report_path, report)
            self._notify_learning_evidence(drift, observed_at)
        except Exception:  # see above: evidence never breaks the cadence
            self._logger.exception("decision_quality_report_failed")

    def _refresh_specialist_skill(self, timestamp: datetime) -> dict | None:
        """Once per IST week: score the specialists, write the report, apply the weights.

        The window ends where the week began, so every tick of the week (and a restart in
        the middle of it) computes the same weights from the same rows. Returns the report
        this tick used, or None when no report file is configured.
        """
        if self.decision_quality_report_path is None:
            return None
        from quant_ai.analytics import specialist_skill as skill

        week = skill.week_start(timestamp).isoformat()
        cached = getattr(self, "_skill_report", None)
        if week == self._skill_week and cached is not None:
            return cached
        report = skill.build_skill_report(self.tracker.broker, tenant_id=self.tenant_id, now=timestamp, until=skill.week_start(timestamp))
        path = skill.report_path(self.decision_quality_report_path)
        skill.write_skill_report(path, report)
        engine = self.scheduler.pipeline.runtime.attribution
        weights = skill.weights_of(report)
        if self.specialist_reweighting and weights:
            engine.apply_skill(weights, basis=report["basis_sha256"])
        else:
            engine.clear_skill()
        self._skill_week, self._skill_report = week, report
        if weights:
            self._notify_specialist_skill(report, path, timestamp)
        return report

    def _notify_specialist_skill(self, report: dict, path: Path, timestamp: datetime) -> None:
        """One note per IST week, across restarts, naming every weight that applies."""
        from quant_ai.analytics.specialist_skill import notification_message
        from quant_ai.notifications.trading import AlertPriority

        marker = path.parent / f".skill-notified-{str(report['week_start'])[:10]}"
        if marker.exists():
            return
        self.notifications.dispatch(
            TradingAlertCode.SPECIALIST_WEIGHTS_UPDATED,
            notification_message(report, applied=self.specialist_reweighting),
            tenant_id=self.tenant_id, priority=AlertPriority.INFO,
            metadata={
                "week_start": str(report["week_start"]),
                "applied": str(report["applied"]),
                "applied_to_engine": str(self.specialist_reweighting).lower(),
                "basis_sha256": str(report["basis_sha256"]),
            },
        )
        marker.write_text(timestamp.isoformat(), encoding="utf-8")

    def _write_post_mortem(self, timestamp: datetime) -> None:
        """Build the latest session's post-mortem once, after its close, and ask for approval.

        The latest session with decisions is reviewed when the venue is outside regular
        hours and an hour has passed since that session's last decision, so the 60-minute
        outcomes the lessons read have had their chance to resolve. A file that already
        exists, pending or approved, is never rebuilt here; the operator's CLI owns that.
        """
        if self.post_mortem_dir is None:
            return
        try:
            from quant_ai.analytics import post_mortem as review

            session_date = review.latest_session_date(self.tracker.broker, tenant_id=self.tenant_id)
            if session_date is None:
                return
            stamp = session_date.isoformat()
            if stamp in self._post_mortems_built:
                return
            path = review.post_mortem_path(self.post_mortem_dir, session_date)
            if path.exists():
                self._post_mortems_built.add(stamp)
                return
            if self.scheduler.calendar.state(
                self.instrument.market, timestamp, exchange=self.instrument.exchange
            ) == MarketState.REGULAR_HOURS:
                return
            rows = review.session_rows(self.tracker.broker, tenant_id=self.tenant_id, session_date=session_date)
            decided = [review.aware(datetime.fromisoformat(str(row["decided_at"]))) for row in rows if row.get("decided_at")]
            # The hour runs from the last decision made while the venue was open; a hold
            # journaled after the close must not keep pushing the review back.
            in_session = [
                moment for moment in decided
                if self.scheduler.calendar.state(
                    self.instrument.market, moment, exchange=self.instrument.exchange
                ) == MarketState.REGULAR_HOURS
            ] or decided
            if not in_session or timestamp < max(in_session) + timedelta(hours=1):
                return
            report = review.build_post_mortem(
                self.tracker.broker, tenant_id=self.tenant_id, now=timestamp, session_date=session_date
            )
            review.write_post_mortem(self.post_mortem_dir, report)
            self._post_mortems_built.add(stamp)
            self._notify_post_mortem(report, path)
        except Exception:  # see above: evidence never breaks the cadence
            self._logger.exception("post_mortem_build_failed")

    def _notify_post_mortem(self, report: dict, path: Path) -> None:
        from quant_ai.notifications.trading import AlertPriority

        lessons = [item for item in report.get("lessons", []) if isinstance(item, str)]
        stamp = report["session_date"]
        head = (
            f"Post-mortem {stamp} written: {len(lessons)} lesson{'s' if len(lessons) != 1 else ''} "
            f"pending your approval. Review {path}, then approve with: pramana post-mortem --approve {stamp}"
        )
        message = "\n".join([head, *lessons[:3]])
        self.notifications.dispatch(
            TradingAlertCode.POST_MORTEM_PENDING, message,
            tenant_id=self.tenant_id, priority=AlertPriority.INFO,
            metadata={"session_date": stamp, "lessons": str(len(lessons)), "path": str(path)},
        )

    def _notify_learning_evidence(self, drift: dict, timestamp: datetime) -> None:
        """Observation alert only; never halt protection or approve a candidate."""
        from quant_ai.analytics.decision_quality import IST
        from quant_ai.notifications.trading import AlertPriority

        signature = (
            timestamp.astimezone(IST).date().isoformat(), drift.get("status"), drift.get("candidate_id"),
            tuple(drift.get("reasons", ())),
        )
        if drift.get("status") not in {"degraded", "refused"}:
            self._learning_alert_signature = None
            return
        if signature == getattr(self, "_learning_alert_signature", None):
            return
        self.notifications.dispatch(
            TradingAlertCode.LEARNING_EVIDENCE_DEGRADED,
            "Learning evidence needs review; no model or risk setting was changed.",
            tenant_id=self.tenant_id, priority=AlertPriority.HIGH,
            metadata={"status": drift["status"], "reasons": ",".join(drift["reasons"])},
        )
        self._learning_alert_signature = signature

    def _write_missed_opportunities(self, timestamp: datetime) -> None:
        """Rewrite the session's missed-opportunity file, when the factory configured a directory."""
        if self.missed_opportunity_dir is None:
            return
        try:
            from quant_ai.analytics import missed_opportunities as missed

            report = missed.build_report(self.tracker.broker, tenant_id=self.tenant_id, now=timestamp)
            missed.write_report(self.missed_opportunity_dir / f"{report['session_date']}.json", report)
            self._notify_missed_opportunities(report, timestamp)
        except Exception:  # see above: evidence never breaks the cadence
            self._logger.exception("missed_opportunity_report_failed")

    def _notify_missed_opportunities(self, report: dict, timestamp: datetime) -> None:
        """Once per session date, after the close: what the day's holds let go by.

        Observation only; nothing here changes a setting. The in-memory set covers the
        running process and the marker file covers a restart, so the founder never gets
        the same day twice. A date with no hold has nothing to report.
        """
        from quant_ai.analytics.missed_opportunities import notification_message
        from quant_ai.notifications.trading import AlertPriority

        session_date = report["session_date"]
        if not report["holds"] or session_date in self._missed_notified:
            return
        if self.scheduler.calendar.state(
            self.instrument.market, timestamp, exchange=self.instrument.exchange
        ) == MarketState.REGULAR_HOURS:
            return
        marker = self.missed_opportunity_dir / f".notified-{session_date}"
        if marker.exists():
            self._missed_notified.add(session_date)  # an earlier process already said it
            return
        self.notifications.dispatch(
            TradingAlertCode.CADENCE_BRIEF,
            notification_message(report),
            tenant_id=self.tenant_id, priority=AlertPriority.INFO,
            metadata={
                "kind": "missed_opportunities",
                "session_date": session_date,
                "holds": str(report["holds"]),
                "evaluated": str(report["evaluated"]),
                "missed": str(report["missed"]),
                "avoided": str(report["avoided"]),
                "unresolved": str(report["unresolved"]),
                "threshold": str(report["threshold"]),
            },
        )
        # Remembered before the marker is written, so a directory that stops taking writes
        # costs the marker and not a second copy of the note.
        self._missed_notified.add(session_date)
        marker.write_text(timestamp.isoformat(), encoding="utf-8")

    def _ai_budget_status(self) -> dict | None:
        """Today's consensus spend headroom, or None when no budget is configured.

        An exhausted budget degrades every consensus to NEUTRAL, so the cadence keeps
        running while the decisions stop being AI-informed. Without this on the page a
        founder sees a quiet engine and no reason for it.
        """
        try:
            client = self.scheduler.pipeline.runtime.cio.atlas.llm_client
            budget = getattr(client, "budget", None)
            if budget is None:
                return None
            from quant_ai.llm.anthropic_client import BUDGET_SCOPE

            return budget.status(BUDGET_SCOPE)
        except Exception:  # see above: evidence never breaks the cadence
            self._logger.exception("ai_budget_status_failed")
            return None

    def _decision_mark(self, symbol: str) -> Decimal | None:
        """Current mark for a journaled symbol from the shared feed; None when unknown."""
        instrument = next((item for item in self.instruments if item.symbol == symbol), None)
        if instrument is None:
            return None
        try:
            return positive_level(self.tracker.market_feed.latest_tick(instrument).last_price)
        except (ValueError, RuntimeError, TimeoutError, ConnectionError, OSError,
                DecimalException, AttributeError, TypeError):
            return None

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
        # The venue rows above are NSE/NYSE hours. A watchlist instrument on MCX is live
        # for eight hours after INDIA reads CLOSED, so each exchange in the book gets its
        # own row and the heartbeat stops implying one session per country.
        sessions.update({
            f"{item.market.value}:{item.exchange}": self.scheduler.calendar.state(
                item.market, timestamp, exchange=item.exchange
            ).value
            for item in self.instruments
        })
        return DaemonHeartbeat(
            timestamp,
            max(0.0, monotonic() - self._started_monotonic),
            self.scheduler.last_run_at,
            sessions,
            self._stop_requested,
        )
