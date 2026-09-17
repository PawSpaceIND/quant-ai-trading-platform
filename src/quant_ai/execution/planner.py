"""Deterministic, liquidity-bounded execution planning.

A strategy decides *what* exposure it wants.  This module decides how a paper/live adapter
may break that approved parent quantity into child intents without inventing liquidity.
It never places an order and never assumes market impact from unavailable data.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal
from enum import Enum

from quant_ai.domain.models import OrderIntent


class ExecutionAlgorithm(str, Enum):
    IMMEDIATE = "IMMEDIATE"
    TWAP = "TWAP"
    VWAP = "VWAP"
    POV = "POV"


@dataclass(frozen=True)
class VolumeBucket:
    at: datetime
    available_quantity: int

    def __post_init__(self) -> None:
        if self.at.tzinfo is None or self.at.utcoffset() is None:
            raise ValueError("execution_bucket_time_must_be_timezone_aware")
        if type(self.available_quantity) is not int or self.available_quantity < 0:
            raise ValueError("execution_bucket_volume_must_be_nonnegative_integer")


@dataclass(frozen=True)
class ExecutionConstraints:
    lot_size: int = 1
    max_participation: Decimal = Decimal("0.10")
    max_child_quantity: int | None = None
    min_child_quantity: int | None = None

    def __post_init__(self) -> None:
        if type(self.lot_size) is not int or self.lot_size < 1:
            raise ValueError("execution_lot_size_must_be_positive_integer")
        if not self.max_participation.is_finite() or not Decimal(0) < self.max_participation <= 1:
            raise ValueError("max_participation_must_be_in_0_1")
        for name, value in (
            ("max_child_quantity", self.max_child_quantity),
            ("min_child_quantity", self.min_child_quantity),
        ):
            if value is not None and (
                type(value) is not int or value < self.lot_size or value % self.lot_size
            ):
                raise ValueError(f"{name}_must_be_whole_lots")
        if (
            self.max_child_quantity is not None
            and self.min_child_quantity is not None
            and self.min_child_quantity > self.max_child_quantity
        ):
            raise ValueError("execution_child_bounds_inverted")


@dataclass(frozen=True)
class ExecutionSlice:
    sequence: int
    at: datetime
    quantity: int
    observed_available_quantity: int | None
    participation: Decimal | None


@dataclass(frozen=True)
class ExecutionPlan:
    algorithm: ExecutionAlgorithm
    parent_quantity: int
    lot_size: int
    slices: tuple[ExecutionSlice, ...]
    source: str

    @property
    def planned_quantity(self) -> int:
        return sum(item.quantity for item in self.slices)

    def assert_conservative(self) -> None:
        if self.planned_quantity != self.parent_quantity:
            raise ValueError("execution_plan_quantity_mismatch")
        if not self.slices:
            raise ValueError("execution_plan_empty")
        if any(item.quantity <= 0 or item.quantity % self.lot_size for item in self.slices):
            raise ValueError("execution_plan_non_lot_quantity")
        if tuple(item.sequence for item in self.slices) != tuple(range(1, len(self.slices) + 1)):
            raise ValueError("execution_plan_sequence_invalid")
        if any(a.at > b.at for a, b in zip(self.slices, self.slices[1:])):
            raise ValueError("execution_plan_time_not_monotonic")


class ExecutionPlanner:
    def plan(
        self,
        order: OrderIntent,
        *,
        algorithm: ExecutionAlgorithm,
        constraints: ExecutionConstraints,
        buckets: tuple[VolumeBucket, ...] = (),
        source: str = "operator supplied execution inputs",
    ) -> ExecutionPlan:
        if type(order.quantity) is not int or order.quantity <= 0:
            raise ValueError("execution_parent_quantity_must_be_positive_integer")
        instrument = getattr(order, "instrument", None)
        if instrument is not None and instrument.lot_size is not None and instrument.lot_size != constraints.lot_size:
            raise ValueError("execution_contract_lot_mismatch")
        if order.quantity % constraints.lot_size:
            raise ValueError("execution_parent_quantity_not_whole_lots")
        if not source.strip():
            raise ValueError("execution_plan_source_required")
        if not buckets:
            raise ValueError("execution_volume_schedule_required")
        if any(a.at >= b.at for a, b in zip(buckets, buckets[1:])):
            raise ValueError("execution_volume_buckets_must_be_strictly_ordered")
        capacity = tuple(self._bucket_capacity(item, constraints) for item in buckets)
        if algorithm is ExecutionAlgorithm.IMMEDIATE:
            slices = self._immediate(order.quantity, buckets, capacity, constraints)
        elif algorithm is ExecutionAlgorithm.TWAP:
            slices = self._twap(order.quantity, buckets, capacity, constraints)
        elif algorithm in {ExecutionAlgorithm.VWAP, ExecutionAlgorithm.POV}:
            # With observed/forecast available volume, both schedules allocate against the
            # declared capacity curve.  VWAP attempts to complete the whole parent across
            # the supplied horizon; POV likewise refuses if that participation cap cannot
            # complete it.  Neither fabricates extra volume after the horizon.
            slices = self._volume_weighted(order.quantity, buckets, capacity, constraints)
        else:  # pragma: no cover - Enum prevents this for typed callers.
            raise ValueError("unknown_execution_algorithm")
        plan = ExecutionPlan(algorithm, order.quantity, constraints.lot_size, slices, source)
        plan.assert_conservative()
        self._assert_participation(plan, constraints)
        return plan

    @staticmethod
    def _bucket_capacity(bucket: VolumeBucket, constraints: ExecutionConstraints) -> int:
        raw = (
            Decimal(bucket.available_quantity) * constraints.max_participation
        ).to_integral_value(rounding=ROUND_FLOOR)
        lots = int(raw) // constraints.lot_size
        quantity = lots * constraints.lot_size
        if constraints.max_child_quantity is not None:
            quantity = min(quantity, constraints.max_child_quantity)
        return quantity

    @staticmethod
    def _minimum(constraints: ExecutionConstraints) -> int:
        return constraints.min_child_quantity or constraints.lot_size

    def _immediate(
        self,
        quantity: int,
        buckets: tuple[VolumeBucket, ...],
        capacity: tuple[int, ...],
        constraints: ExecutionConstraints,
    ) -> tuple[ExecutionSlice, ...]:
        if quantity > capacity[0]:
            raise ValueError("execution_immediate_capacity_insufficient")
        if quantity < self._minimum(constraints):
            raise ValueError("execution_child_below_minimum")
        return (self._slice(1, buckets[0], quantity),)

    def _twap(
        self,
        quantity: int,
        buckets: tuple[VolumeBucket, ...],
        capacity: tuple[int, ...],
        constraints: ExecutionConstraints,
    ) -> tuple[ExecutionSlice, ...]:
        lot = constraints.lot_size
        total_lots = quantity // lot
        base, remainder = divmod(total_lots, len(buckets))
        desired = tuple((base + (1 if index < remainder else 0)) * lot for index in range(len(buckets)))
        if any(target > cap for target, cap in zip(desired, capacity)):
            raise ValueError("execution_twap_capacity_insufficient")
        minimum = self._minimum(constraints)
        chosen = [(bucket, target) for bucket, target in zip(buckets, desired) if target]
        if any(target < minimum for _, target in chosen):
            raise ValueError("execution_child_below_minimum")
        return tuple(self._slice(index, bucket, target) for index, (bucket, target) in enumerate(chosen, 1))

    def _volume_weighted(
        self,
        quantity: int,
        buckets: tuple[VolumeBucket, ...],
        capacity: tuple[int, ...],
        constraints: ExecutionConstraints,
    ) -> tuple[ExecutionSlice, ...]:
        if sum(capacity) < quantity:
            raise ValueError("execution_participation_capacity_insufficient")
        remaining = quantity
        selected: list[tuple[VolumeBucket, int]] = []
        minimum = self._minimum(constraints)
        for bucket, cap in zip(buckets, capacity):
            if remaining <= 0:
                break
            take = min(cap, remaining)
            if take == 0:
                continue
            if take < minimum and remaining != take:
                continue
            selected.append((bucket, take))
            remaining -= take
        if remaining:
            # The simple greedy allocation can leave a final sub-minimum remainder even
            # though total capacity was sufficient. Rebalance one whole minimum block from
            # an earlier child before giving up.
            if remaining < minimum and selected:
                needed = minimum - remaining
                for index in range(len(selected) - 1, -1, -1):
                    bucket, take = selected[index]
                    if take - needed >= minimum:
                        selected[index] = (bucket, take - needed)
                        remaining += needed
                        break
            if remaining:
                next_index = len(selected)
                for bucket, cap in zip(buckets[next_index:], capacity[next_index:]):
                    if cap >= remaining >= minimum:
                        selected.append((bucket, remaining))
                        remaining = 0
                        break
        if remaining:
            raise ValueError("execution_child_minimum_prevents_completion")
        return tuple(self._slice(index, bucket, take) for index, (bucket, take) in enumerate(selected, 1))

    @staticmethod
    def _slice(sequence: int, bucket: VolumeBucket, quantity: int) -> ExecutionSlice:
        participation = (
            Decimal(quantity) / Decimal(bucket.available_quantity)
            if bucket.available_quantity > 0
            else None
        )
        return ExecutionSlice(
            sequence, bucket.at, quantity, bucket.available_quantity, participation
        )

    @staticmethod
    def _assert_participation(plan: ExecutionPlan, constraints: ExecutionConstraints) -> None:
        for item in plan.slices:
            if item.observed_available_quantity is None or item.participation is None:
                raise ValueError("execution_plan_missing_volume_evidence")
            if item.participation > constraints.max_participation:
                raise ValueError("execution_plan_participation_limit_exceeded")
