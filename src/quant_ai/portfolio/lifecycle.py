"""Deterministic position lifecycle: protection, time exits and controlled scaling.

This module proposes position-management actions. It never submits an order and a proposed
ADD must still pass the normal portfolio/risk/OMS path. Protective exits and max-holding
rules dominate every scale-in rule, protection can only tighten, and scale-in triggers are
only defined on favourable moves (no automated averaging down).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from enum import Enum


class PositionDirection(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class LifecycleAction(str, Enum):
    HOLD = "HOLD"
    EXIT_ALL = "EXIT_ALL"
    REDUCE = "REDUCE"
    ADD = "ADD"


@dataclass(frozen=True)
class ScaleRule:
    rule_id: str
    gain_fraction: Decimal
    quantity_fraction: Decimal

    def __post_init__(self) -> None:
        if not self.rule_id.strip():
            raise ValueError("position_scale_rule_id_required")
        if not self.gain_fraction.is_finite() or self.gain_fraction <= 0:
            raise ValueError("position_scale_gain_must_be_positive")
        if not self.quantity_fraction.is_finite() or not Decimal(0) < self.quantity_fraction <= 1:
            raise ValueError("position_scale_fraction_must_be_in_0_1")


@dataclass(frozen=True)
class PositionLifecyclePolicy:
    lot_size: int = 1
    max_holding_period: timedelta | None = None
    break_even_activation_gain: Decimal | None = None
    trailing_activation_gain: Decimal | None = None
    trailing_distance_fraction: Decimal | None = None
    scale_out_rules: tuple[ScaleRule, ...] = ()
    scale_in_rules: tuple[ScaleRule, ...] = ()
    max_position_quantity: int | None = None

    def __post_init__(self) -> None:
        if type(self.lot_size) is not int or self.lot_size < 1:
            raise ValueError("position_lifecycle_lot_size_invalid")
        if self.max_holding_period is not None and self.max_holding_period <= timedelta(0):
            raise ValueError("max_holding_period_must_be_positive")
        for name in ("break_even_activation_gain", "trailing_activation_gain"):
            value = getattr(self, name)
            if value is not None and (not value.is_finite() or value <= 0):
                raise ValueError(f"{name}_must_be_positive")
        if self.trailing_distance_fraction is not None and (
            not self.trailing_distance_fraction.is_finite()
            or not Decimal(0) < self.trailing_distance_fraction < 1
        ):
            raise ValueError("trailing_distance_fraction_must_be_in_0_1")
        if (self.trailing_activation_gain is None) != (self.trailing_distance_fraction is None):
            raise ValueError("trailing_activation_and_distance_must_be_configured_together")
        all_rules = (*self.scale_out_rules, *self.scale_in_rules)
        ids = [item.rule_id for item in all_rules]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate_position_scale_rule")
        for rules in (self.scale_out_rules, self.scale_in_rules):
            gains = [item.gain_fraction for item in rules]
            if gains != sorted(gains):
                raise ValueError("position_scale_rules_must_be_gain_ordered")
        if self.max_position_quantity is not None and (
            type(self.max_position_quantity) is not int
            or self.max_position_quantity < self.lot_size
            or self.max_position_quantity % self.lot_size
        ):
            raise ValueError("max_position_quantity_must_be_whole_lots")


@dataclass(frozen=True)
class PositionLifecycleState:
    symbol: str
    direction: PositionDirection
    quantity: int
    initial_quantity: int
    average_entry_price: Decimal
    opened_at: datetime
    favourable_extreme: Decimal
    stop_price: Decimal | None = None
    take_profit_price: Decimal | None = None
    completed_rules: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise ValueError("position_lifecycle_symbol_required")
        if type(self.quantity) is not int or self.quantity <= 0:
            raise ValueError("position_lifecycle_quantity_must_be_positive_integer")
        if type(self.initial_quantity) is not int or self.initial_quantity <= 0:
            raise ValueError("position_lifecycle_initial_quantity_must_be_positive_integer")
        if not self.average_entry_price.is_finite() or self.average_entry_price <= 0:
            raise ValueError("position_lifecycle_entry_price_invalid")
        if not self.favourable_extreme.is_finite() or self.favourable_extreme <= 0:
            raise ValueError("position_lifecycle_extreme_invalid")
        if self.opened_at.tzinfo is None or self.opened_at.utcoffset() is None:
            raise ValueError("position_lifecycle_opened_at_must_be_timezone_aware")
        for value in (self.stop_price, self.take_profit_price):
            if value is not None and (not value.is_finite() or value <= 0):
                raise ValueError("position_lifecycle_protective_level_invalid")
        if self.direction is PositionDirection.LONG:
            if self.stop_price is not None and self.stop_price >= self.average_entry_price:
                # A breakeven/profit stop can become valid later. At state creation callers
                # may legitimately carry it; therefore only nonsensical stop-vs-target
                # geometry is rejected below, not stop-vs-entry.
                pass
            if (
                self.stop_price is not None
                and self.take_profit_price is not None
                and self.stop_price >= self.take_profit_price
            ):
                raise ValueError("position_lifecycle_protection_inverted")
        elif (
            self.stop_price is not None
            and self.take_profit_price is not None
            and self.stop_price <= self.take_profit_price
        ):
            raise ValueError("position_lifecycle_protection_inverted")


@dataclass(frozen=True)
class PositionLifecycleDecision:
    action: LifecycleAction
    quantity: int
    reason: str
    proposed_stop_price: Decimal | None
    favourable_extreme: Decimal
    completed_rule: str | None = None


class PositionLifecycleManager:
    def evaluate(
        self,
        state: PositionLifecycleState,
        *,
        mark_price: Decimal,
        now: datetime,
        policy: PositionLifecyclePolicy,
    ) -> PositionLifecycleDecision:
        if not mark_price.is_finite() or mark_price <= 0:
            raise ValueError("position_lifecycle_mark_invalid")
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("position_lifecycle_now_must_be_timezone_aware")
        if state.quantity % policy.lot_size or state.initial_quantity % policy.lot_size:
            raise ValueError("position_lifecycle_state_not_whole_lots")
        if now < state.opened_at:
            raise ValueError("position_lifecycle_now_before_open")
        extreme = self._updated_extreme(state, mark_price)
        if self._stop_hit(state, mark_price):
            return self._decision(
                LifecycleAction.EXIT_ALL, state.quantity, "protective_stop_hit", state.stop_price,
                extreme,
            )
        if self._target_hit(state, mark_price):
            return self._decision(
                LifecycleAction.EXIT_ALL, state.quantity, "take_profit_hit", state.stop_price,
                extreme,
            )
        if (
            policy.max_holding_period is not None
            and now - state.opened_at >= policy.max_holding_period
        ):
            return self._decision(
                LifecycleAction.EXIT_ALL, state.quantity, "max_holding_period_reached",
                state.stop_price, extreme,
            )
        gain = self._gain(state, mark_price)
        proposed_stop = self._tightened_stop(state, extreme, gain, policy)
        for rule in policy.scale_out_rules:
            if rule.rule_id in state.completed_rules or gain < rule.gain_fraction:
                continue
            quantity = min(state.quantity, self._rule_quantity(state.initial_quantity, rule, policy))
            if quantity <= 0:
                continue
            action = LifecycleAction.EXIT_ALL if quantity == state.quantity else LifecycleAction.REDUCE
            return self._decision(
                action, quantity, f"scale_out:{rule.rule_id}", proposed_stop, extreme, rule.rule_id
            )
        for rule in policy.scale_in_rules:
            if rule.rule_id in state.completed_rules or gain < rule.gain_fraction:
                continue
            # Scale-in is only permitted when the existing position is protected. It is still
            # only a proposal; the portfolio/risk firewall must approve the resulting ADD.
            if state.stop_price is None and proposed_stop is None:
                continue
            quantity = self._rule_quantity(state.initial_quantity, rule, policy)
            if quantity <= 0:
                continue
            if (
                policy.max_position_quantity is not None
                and state.quantity + quantity > policy.max_position_quantity
            ):
                continue
            return self._decision(
                LifecycleAction.ADD, quantity, f"scale_in:{rule.rule_id}", proposed_stop,
                extreme, rule.rule_id,
            )
        return self._decision(
            LifecycleAction.HOLD, 0,
            "protection_tightened" if proposed_stop != state.stop_price else "hold",
            proposed_stop, extreme,
        )

    @staticmethod
    def _updated_extreme(state: PositionLifecycleState, mark: Decimal) -> Decimal:
        if state.direction is PositionDirection.LONG:
            return max(state.favourable_extreme, mark)
        return min(state.favourable_extreme, mark)

    @staticmethod
    def _gain(state: PositionLifecycleState, mark: Decimal) -> Decimal:
        if state.direction is PositionDirection.LONG:
            return (mark - state.average_entry_price) / state.average_entry_price
        return (state.average_entry_price - mark) / state.average_entry_price

    @staticmethod
    def _stop_hit(state: PositionLifecycleState, mark: Decimal) -> bool:
        if state.stop_price is None:
            return False
        if state.direction is PositionDirection.LONG:
            return mark <= state.stop_price
        return mark >= state.stop_price

    @staticmethod
    def _target_hit(state: PositionLifecycleState, mark: Decimal) -> bool:
        if state.take_profit_price is None:
            return False
        if state.direction is PositionDirection.LONG:
            return mark >= state.take_profit_price
        return mark <= state.take_profit_price

    def _tightened_stop(
        self,
        state: PositionLifecycleState,
        extreme: Decimal,
        gain: Decimal,
        policy: PositionLifecyclePolicy,
    ) -> Decimal | None:
        candidates: list[Decimal] = []
        if state.stop_price is not None:
            candidates.append(state.stop_price)
        if (
            policy.break_even_activation_gain is not None
            and gain >= policy.break_even_activation_gain
        ):
            candidates.append(state.average_entry_price)
        if (
            policy.trailing_activation_gain is not None
            and policy.trailing_distance_fraction is not None
            and gain >= policy.trailing_activation_gain
        ):
            if state.direction is PositionDirection.LONG:
                candidates.append(extreme * (Decimal(1) - policy.trailing_distance_fraction))
            else:
                candidates.append(extreme * (Decimal(1) + policy.trailing_distance_fraction))
        if not candidates:
            return None
        if state.direction is PositionDirection.LONG:
            return max(candidates)
        return min(candidates)

    @staticmethod
    def _rule_quantity(
        initial_quantity: int, rule: ScaleRule, policy: PositionLifecyclePolicy
    ) -> int:
        raw = (Decimal(initial_quantity) * rule.quantity_fraction).to_integral_value(
            rounding=ROUND_FLOOR
        )
        return (int(raw) // policy.lot_size) * policy.lot_size

    @staticmethod
    def _decision(
        action: LifecycleAction,
        quantity: int,
        reason: str,
        stop: Decimal | None,
        extreme: Decimal,
        completed_rule: str | None = None,
    ) -> PositionLifecycleDecision:
        return PositionLifecycleDecision(action, quantity, reason, stop, extreme, completed_rule)
