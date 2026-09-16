from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, DecimalException
from enum import Enum
from typing import Callable
from uuid import uuid4

from quant_ai.brokers.adapter import BrokerPosition
from quant_ai.brokers.base import ExecutionResult
from quant_ai.domain.models import (
    AssetClass,
    Instrument,
    InstrumentBoundOrderIntent,
    Market,
    OrderIntent,
    Side,
)
from quant_ai.execution.overnight import OvernightGapMonitor
from quant_ai.execution.paper_ledger import (
    PaperBrokerDatabaseLockedError,
    PaperBrokerService,
)
from quant_ai.execution.protection_state import positive_level
from quant_ai.marketdata.corporate_calendar import (
    DEFAULT_DISCONTINUITY_FRACTION,
    price_discontinuity,
)
from quant_ai.marketdata.gap import GapAssessment, GapVerdict
from quant_ai.notifications.trading import (
    TradingAlertCode,
    TradingNotificationDispatcher,
)

LOGGER = logging.getLogger("quant_ai.protective_exits")

MarkResolver = Callable[[BrokerPosition], Decimal | None]


class ExitTrigger(str, Enum):
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"


@dataclass(frozen=True)
class ProtectiveExit:
    """One liquidation decision, whether or not the resulting order filled."""

    symbol: str
    market: Market
    asset_class: AssetClass
    quantity: int
    trigger: ExitTrigger
    threshold: Decimal
    mark_price: Decimal
    average_price: Decimal
    filled: bool
    order_id: str | None = None
    reason: str = ""

    @property
    def realized_pnl(self) -> Decimal:
        return (self.mark_price - self.average_price) * self.quantity


# Returned by ``_breach`` when a protected position has no usable mark. Distinct from
# ``None`` ("priced, no breach") so a feed outage cannot be mistaken for a quiet market.
MARK_UNAVAILABLE = object()

# Returned when the quote was re-based by a corporate action: the stored stop and cost
# basis refer to a different unit, so the comparison is meaningless, not breached.
PRICE_REBASED = object()


class ProtectiveExitEngine:
    """Active stop-loss / take-profit monitor.

    The risk firewall only proves a protective level was *calculated*. This component is what
    makes the level binding: it marks every open position against the live price and liquidates
    the full position the moment a stored threshold is breached. It is deliberately independent
    of the swarm — no agent vote, no LLM call and no consensus can suppress an exit.
    """

    #: Symbols the most recent sweep could not price. Empty before the first evaluate().
    unprotected: tuple[str, ...] = ()

    #: Symbols whose quote was re-based by a corporate action during the most recent sweep.
    rebased: tuple[str, ...] = ()

    #: Declared ex-dates. None means undeclared actions are caught by the step guard alone.
    corporate_calendar: object | None = None
    discontinuity_fraction: Decimal = DEFAULT_DISCONTINUITY_FRACTION

    def __init__(
        self,
        broker: PaperBrokerService,
        mark_resolver: MarkResolver,
        *,
        tenant_id: str = "default",
        dispatcher: TradingNotificationDispatcher | None = None,
        strategy_id: str = "protective-exit",
        re_entry_cooldown: timedelta = timedelta(minutes=30),
        corporate_calendar: object | None = None,
        discontinuity_fraction: Decimal = DEFAULT_DISCONTINUITY_FRACTION,
        gap_monitor: OvernightGapMonitor | None = None,
    ) -> None:
        self.broker = broker
        self.mark_resolver = mark_resolver
        self.strategy_manifest_provider = None
        self.tenant_id = tenant_id
        self.dispatcher = dispatcher or TradingNotificationDispatcher()
        self.strategy_id = strategy_id
        # Without a cooldown a stop-out is re-entered on the very next evaluation, which in a
        # sustained decline turns one contained loss into a repeated one. Set to timedelta(0)
        # to disable.
        self.re_entry_cooldown = re_entry_cooldown
        # A corporate action re-bases the quote without changing what the position is worth,
        # so the stored stop and cost basis stop being comparable for that session. Declared
        # ex-dates say so; the step guard catches the ones nobody declared.
        self.corporate_calendar = corporate_calendar
        self.discontinuity_fraction = discontinuity_fraction
        self._last_mark: dict[str, Decimal] = {}
        self._suspended: set[str] = set()
        self._sweep_at: datetime | None = None
        # Classifies the same step the guard above measures, and keeps an unexplained one
        # in front of the operator. It may narrow a suspension - never widen one, and never
        # suppress an exit the stored levels call for. See ``_rebased``.
        self.gap_monitor = gap_monitor

    @property
    def last_sweep_at(self) -> datetime | None:
        """When the most recent sweep ran, or None before the first ``evaluate``.

        ``unprotected`` and ``rebased`` are empty both before the first sweep and after a
        clean one, so anything reporting them to an operator needs this to tell "nothing
        found" apart from "not looked yet". The two must never read the same.
        """
        return self._sweep_at

    def evaluate(self, now: datetime | None = None) -> tuple[ProtectiveExit, ...]:
        """Check every open position and liquidate the ones whose thresholds are breached.

        Positions the engine could not price are recorded in ``unprotected``. Skipping an
        unknown mark is the safe choice for *this* sweep, but a position whose stop cannot
        be evaluated is unprotected in fact, and silence about that is how a feed outage
        turns a stop-protected book into a naked one. The caller decides how long to
        tolerate it.
        """
        observed_at = now or datetime.now(timezone.utc)
        self._sweep_at = observed_at
        exits: list[ProtectiveExit] = []
        unprotected: list[str] = []
        rebased: list[str] = []
        held: list[str] = []
        for position in self.broker.get_protection_positions(self.tenant_id):
            held.append(position.symbol)
            decision = self._breach(position)
            if decision is MARK_UNAVAILABLE:
                unprotected.append(position.symbol)
                continue
            if decision is PRICE_REBASED:
                rebased.append(position.symbol)
                continue
            if decision is None:
                continue
            trigger, threshold, mark = decision
            exits.append(self._liquidate(position, trigger, threshold, mark, observed_at))
        self.unprotected = tuple(unprotected)
        self.rebased = tuple(rebased)
        if self.gap_monitor is not None:
            # A position that has left the book cannot still be carrying an unexplained
            # gap, so it stops escalating. Symbols this sweep liquidated are dropped with
            # it; an exit that failed to fill is still held and still escalates.
            closed = {item.symbol for item in exits if item.filled}
            self.gap_monitor.forget(tuple(item for item in held if item not in closed))
        return tuple(exits)

    def _breach(
        self, position: BrokerPosition
    ) -> tuple[ExitTrigger, Decimal, Decimal] | None | object:
        if position.quantity <= 0:
            return None
        stop, target = positive_level(position.stop_price), positive_level(position.take_profit_price)
        if stop is None and target is None:
            return None
        mark = self._mark(position)
        if mark is None:
            return MARK_UNAVAILABLE
        if self._rebased(position, mark, self._observe_gap(position, mark)):
            return PRICE_REBASED
        # Long-only ledger: a stop sits below entry and a target above it. The stop is
        # evaluated first so a bar that spans both thresholds resolves conservatively.
        if stop is not None and mark <= stop:
            return ExitTrigger.STOP_LOSS, stop, mark
        if target is not None and mark >= target:
            return ExitTrigger.TAKE_PROFIT, target, mark
        return None

    def _rebased(
        self, position: BrokerPosition, mark: Decimal, assessment: GapAssessment | None
    ) -> bool:
        """True when this quote cannot be compared with the stored stop and cost basis.

        A declared ex-date says so outright. Failing that, a step larger than the exchange
        band means the quote was re-based by an action nobody recorded. Either way the
        stored levels refer to a different unit, and acting on the comparison would book a
        loss the market never caused.

        The step guard is a size test, and a size test cannot tell a 1:5 split from a
        company that lost four fifths of its value: both take the quote to a fifth of where
        it was. So a catastrophic gap is silenced by the same rule that protects a split -
        precisely the gap that most needs the stop. ``assessment`` is the one thing that
        can narrow that, and only where it can *prove* an action is impossible: a corporate
        action re-bases at an open, so a step this large between two marks inside one
        session was not one, and the quote still refers to the same unit the stop does.
        Suspending there would leave a genuinely collapsing position naked.

        Nothing the assessment says ever *creates* a suspension, and it cannot reach the
        declared-ex-date branch at all - a declaration classifies as ``DECLARED_ACTION``,
        never as an intrasession break. Where a split and a crash are genuinely
        indistinguishable the suspension stands, and the monitor keeps it in front of an
        operator until they close it.
        """
        symbol = position.symbol
        previous = self._last_mark.get(symbol)
        # Deliberately not updated here. The stored mark is the last quote that was
        # comparable with the cost basis, so overwriting it with a re-based one erases the
        # evidence: the next sweep would compare the new price with itself, find no step,
        # and liquidate against the old basis - the fabricated loss this guard exists to
        # prevent, one sweep late. Keeping it means the suspension holds until the quote
        # and the basis agree again, which is a human reconciling the position.
        if self.corporate_calendar is not None:
            try:
                declared = self.corporate_calendar.action_on(symbol, self._sweep_at)
            except (ValueError, AttributeError):
                declared = None
            if declared:
                self._log_suspension(
                    symbol,
                    "protective_exit_suspended symbol=%s reason=declared_corporate_action:%s",
                    symbol, declared,
                )
                return True
        if price_discontinuity(previous, mark, fraction=self.discontinuity_fraction):
            if assessment is not None and assessment.verdict is GapVerdict.INTRASESSION_BREAK:
                LOGGER.warning(
                    "protective_exit_armed_through_step symbol=%s reason=%s previous=%s current=%s",
                    symbol, assessment.describe(), previous, mark,
                )
                # The unit did not change, so this quote is the new comparison basis.
                self._last_mark[symbol] = mark
                return False
            self._log_suspension(
                symbol,
                "protective_exit_suspended symbol=%s reason=price_rebased previous=%s current=%s",
                symbol, previous, mark,
            )
            return True
        self._suspended.discard(symbol)
        self._last_mark[symbol] = mark
        return False

    def _log_suspension(self, symbol: str, message: str, *args: object) -> None:
        """Warn once per suspension, not once per sweep.

        The sweep runs every second and a suspension now holds until an operator acts, so
        logging each one would bury the line that matters under thousands of copies of
        itself. Escalation past this first line is the gap monitor's job.
        """
        if symbol in self._suspended:
            return
        self._suspended.add(symbol)
        LOGGER.warning(message, *args)

    def _observe_gap(
        self, position: BrokerPosition, mark: Decimal
    ) -> GapAssessment | None:
        """Classify what this sweep priced and put an unexplained step before the operator.

        The verdict travels back for one narrow purpose only, described in ``_rebased``:
        it may re-arm a stop the step guard would have suspended, never suspend one the
        stored levels say is breached.
        """
        if self.gap_monitor is None or self._sweep_at is None:
            return None
        return self.gap_monitor.observe(
            position.symbol, position.market, mark, self._sweep_at
        )

    def _mark(self, position: BrokerPosition) -> Decimal | None:
        try:
            return positive_level(self.mark_resolver(position))
        except (RuntimeError, ValueError, DecimalException, TimeoutError, ConnectionError, OSError) as error:
            # An unknown price is never treated as a safe price: skip, log, retry next tick.
            LOGGER.warning(
                "protective_exit_mark_unavailable symbol=%s error=%s", position.symbol, error
            )
            return None

    def _liquidate(
        self,
        position: BrokerPosition,
        trigger: ExitTrigger,
        threshold: Decimal,
        mark: Decimal,
        now: datetime,
    ) -> ProtectiveExit:
        order = OrderIntent(
            position.symbol,
            position.market,
            Side.SELL,
            position.quantity,
            mark,
            self.strategy_id,
            position.asset_class,
            self.tenant_id,
            stop_price=positive_level(position.stop_price),
            take_profit_price=positive_level(position.take_profit_price),
        )
        observation = getattr(self.mark_resolver, "last_observation", {})
        if not isinstance(observation, dict) or observation.get("symbol") != position.symbol or observation.get("price") != str(mark):
            observation = {"symbol": position.symbol, "price": str(mark),
                "source": "custom_resolver_unverified", "source_timestamp": None}
        observation = {"symbol": position.symbol, "price": str(mark),
            "source": str(observation.get("source", "unavailable"))[:128],
            "source_timestamp": str(observation["source_timestamp"]) if observation.get("source_timestamp") else None}
        binding = None
        if self.strategy_manifest_provider is not None:
            try:
                binding = self.strategy_manifest_provider(now)
            except Exception as error:  # noqa: BLE001 - metadata failure must never suppress protection
                LOGGER.warning("protective_strategy_evidence_unavailable error=%s", type(error).__name__)
                binding = {"status": "unavailable", "issues": ["evidence_capture_failed"]}
        proof = {
            "runtime_strategy": binding,
            "schema": "pramana.protective_exit.v1", "event_type": "protective_exit",
            "decision_id": "PROTECTION-" + uuid4().hex,
            "generated_at": now.isoformat(), "trigger": trigger.value, "threshold": str(threshold),
            "mark_observation": observation,
            "proposal": {"side": "SELL", "quantity": str(position.quantity), "reference_price": str(mark)},
            "declared_rationales": [f"Deterministic {trigger.value}: observed mark {mark} crossed stored threshold {threshold}.",
                f"Price source: {observation.get('source')}; source timestamp: {observation.get('source_timestamp') or 'unavailable'}.",
                "Covered paper liquidation independent of AI votes. Fill includes broker friction; stop price is not guaranteed."],
            "risk_verdict": {"approved": "true", "reason": "covered_protective_liquidation"},
            "stress_verdict": {"passed": "not_applicable", "reason": "risk_reducing_exit; no AI stress vote"},
        }
        cooldown_until = now + self.re_entry_cooldown if self.re_entry_cooldown > timedelta(0) else None
        try:
            instrument = self.broker.bound_instrument_for_position(
                position.symbol, position.market, position.asset_class, self.tenant_id
            )
            if instrument is not None:
                order = InstrumentBoundOrderIntent(**vars(order), instrument=instrument)
            fill: ExecutionResult = self.broker.sell_protected(order, proof, cooldown_until)
        except (KeyError, TypeError, ValueError, PaperBrokerDatabaseLockedError) as error:
            LOGGER.error(
                "protective_exit_failed symbol=%s trigger=%s error=%s",
                position.symbol,
                trigger.value,
                error,
            )
            self._notify(position, trigger, threshold, mark, filled=False, detail=str(error))
            return ProtectiveExit(
                position.symbol, position.market, position.asset_class, position.quantity,
                trigger, threshold, mark, position.average_price, False, None, str(error),
            )
        LOGGER.info(
            "protective_exit_executed symbol=%s trigger=%s threshold=%s mark=%s order=%s",
            position.symbol, trigger.value, threshold, mark, fill.order_id,
        )
        self._notify(position, trigger, threshold, mark, filled=True, detail=fill.order_id)
        return ProtectiveExit(
            position.symbol, position.market, position.asset_class, position.quantity,
            trigger, threshold, mark, position.average_price, True, fill.order_id, "executed",
        )

    def _notify(
        self,
        position: BrokerPosition,
        trigger: ExitTrigger,
        threshold: Decimal,
        mark: Decimal,
        *,
        filled: bool,
        detail: str,
    ) -> None:
        code = (
            TradingAlertCode.STOP_LOSS_TRIGGERED
            if trigger == ExitTrigger.STOP_LOSS
            else TradingAlertCode.TAKE_PROFIT_TRIGGERED
        )
        verb = "liquidated" if filled else "FAILED to liquidate"
        self.dispatcher.dispatch(
            code,
            f"{trigger.value} {verb} {position.symbol} x{position.quantity} "
            f"at {mark} (threshold {threshold})",
            tenant_id=self.tenant_id,
            metadata={
                "symbol": position.symbol,
                "trigger": trigger.value,
                "threshold": str(threshold),
                "mark_price": str(mark),
                "quantity": str(position.quantity),
                "filled": str(filled),
                "detail": detail,
            },
        )


def market_feed_mark_resolver(
    market_feed: object,
    instrument_resolver: Callable[[BrokerPosition], Instrument],
    tick_reader: object | None = None,
    clock: Callable[[], datetime] | None = None,
) -> MarkResolver:
    """Resolve the mark used to test protective thresholds.

    Prefers the live websocket last-traded price, which is the only genuinely real-time
    price in the build; falls back to the market feed when no fresh tick is buffered.
    A stale tick is rejected by the reader rather than trusted.
    """
    now = clock or (lambda: datetime.now(timezone.utc))

    def resolve(position: BrokerPosition) -> Decimal | None:
        resolve.last_observation = {}  # type: ignore[attr-defined]
        if tick_reader is not None:
            tick, veto = tick_reader.market_data_status(position.symbol, now())  # type: ignore[attr-defined]
            age = None
            if tick is not None:
                observed = tick.observed_at
                current = now()
                if observed.tzinfo is None:
                    observed = observed.replace(tzinfo=timezone.utc)
                if current.tzinfo is None:
                    current = current.replace(tzinfo=timezone.utc)
                age = (current - observed).total_seconds()
            if tick is not None and age is not None and 0 <= age <= 120 and positive_level(tick.ltp) is not None:
                resolve.last_observation = {"symbol": position.symbol, "price": str(tick.ltp),  # type: ignore[attr-defined]
                    "source": tick.source, "source_timestamp": tick.observed_at.isoformat()}
                return tick.ltp
            # A live source is configured but has nothing fresh. The historical feed is not
            # a substitute for it - in the ghost wiring that feed is synthetic - and a
            # fabricated mark can liquidate a healthy position or bank a fictional target.
            # Unknown price -> skip this sweep; the next fresh tick re-arms the check.
            LOGGER.warning(
                "protective_exit_mark_skipped symbol=%s reason=%s", position.symbol, veto
            )
            return None
        tick = market_feed.latest_tick(instrument_resolver(position))  # type: ignore[attr-defined]
        age = (now() - tick.timestamp).total_seconds()
        if not 0 <= age <= 120:
            return None
        resolve.last_observation = {"symbol": position.symbol, "price": str(tick.last_price),  # type: ignore[attr-defined]
            "source": type(market_feed).__name__, "source_timestamp": tick.timestamp.isoformat()}
        return tick.last_price

    return resolve
