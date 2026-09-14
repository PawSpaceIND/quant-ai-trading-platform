from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Callable
from uuid import uuid4

from quant_ai.brokers.adapter import BrokerPosition
from quant_ai.brokers.base import ExecutionResult
from quant_ai.domain.models import AssetClass, Instrument, Market, OrderIntent, Side
from quant_ai.execution.paper_ledger import (
    PaperBrokerDatabaseLockedError,
    PaperBrokerService,
)
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


class ProtectiveExitEngine:
    """Active stop-loss / take-profit monitor.

    The risk firewall only proves a protective level was *calculated*. This component is what
    makes the level binding: it marks every open position against the live price and liquidates
    the full position the moment a stored threshold is breached. It is deliberately independent
    of the swarm — no agent vote, no LLM call and no consensus can suppress an exit.
    """

    def __init__(
        self,
        broker: PaperBrokerService,
        mark_resolver: MarkResolver,
        *,
        tenant_id: str = "default",
        dispatcher: TradingNotificationDispatcher | None = None,
        strategy_id: str = "protective-exit",
        re_entry_cooldown: timedelta = timedelta(minutes=30),
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

    def evaluate(self, now: datetime | None = None) -> tuple[ProtectiveExit, ...]:
        """Check every open position and liquidate the ones whose thresholds are breached."""
        observed_at = now or datetime.now(timezone.utc)
        exits: list[ProtectiveExit] = []
        for position in self.broker.get_positions(self.tenant_id):
            decision = self._breach(position)
            if decision is None:
                continue
            trigger, threshold, mark = decision
            exits.append(self._liquidate(position, trigger, threshold, mark, observed_at))
        return tuple(exits)

    def _breach(
        self, position: BrokerPosition
    ) -> tuple[ExitTrigger, Decimal, Decimal] | None:
        if position.quantity <= 0:
            return None
        if position.stop_price is None and position.take_profit_price is None:
            return None
        mark = self._mark(position)
        if mark is None or mark <= 0:
            return None
        # Long-only ledger: a stop sits below entry and a target above it. The stop is
        # evaluated first so a bar that spans both thresholds resolves conservatively.
        if position.stop_price is not None and mark <= position.stop_price:
            return ExitTrigger.STOP_LOSS, position.stop_price, mark
        if position.take_profit_price is not None and mark >= position.take_profit_price:
            return ExitTrigger.TAKE_PROFIT, position.take_profit_price, mark
        return None

    def _mark(self, position: BrokerPosition) -> Decimal | None:
        try:
            return self.mark_resolver(position)
        except (RuntimeError, ValueError, TimeoutError, ConnectionError, OSError) as error:
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
            stop_price=position.stop_price,
            take_profit_price=position.take_profit_price,
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
            fill: ExecutionResult = self.broker.sell_protected(order, proof, cooldown_until)
        except (ValueError, PaperBrokerDatabaseLockedError) as error:
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
            if tick is not None and tick.ltp > 0:
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
