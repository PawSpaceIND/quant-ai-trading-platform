from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from quant_ai.brokers.base import ExecutionResult
from quant_ai.domain.models import OrderIntent, Side


@dataclass(frozen=True)
class PaperFillObservation:
    """Observed microstructure inputs available when a paper order reaches the venue."""

    available_quantity: int
    queue_ahead_quantity: int = 0
    latency_ms: int = 0
    rejected: bool = False
    reject_reason: str | None = None

    def __post_init__(self) -> None:
        if type(self.available_quantity) is not int or self.available_quantity < 0:
            raise ValueError("available_quantity must be a non-negative integer")
        if type(self.queue_ahead_quantity) is not int or self.queue_ahead_quantity < 0:
            raise ValueError("queue_ahead_quantity must be a non-negative integer")
        if type(self.latency_ms) is not int or self.latency_ms < 0:
            raise ValueError("latency_ms must be a non-negative integer")
        if self.rejected and not (self.reject_reason or "").strip():
            raise ValueError("rejected observation requires reject_reason")


@dataclass(frozen=True)
class PaperFillSimulation:
    result: ExecutionResult
    requested_quantity: int
    fillable_quantity: int
    available_quantity: int
    queue_ahead_quantity: int
    latency_ms: int
    reject_reason: str | None = None


class PaperFillLifecycleSimulator:
    """Deterministic paper venue model for fill lifecycle realism.

    It does not sleep or use randomness. Latency is represented as an adverse price
    penalty, queue position consumes observed capacity before this order, and only the
    remaining quantity can fill. The caller supplies the observation, which keeps replay
    tests reproducible and makes every assumption inspectable.
    """

    def __init__(
        self,
        *,
        base_slippage_bps: Decimal = Decimal(1),
        latency_penalty_bps_per_second: Decimal = Decimal("0.25"),
    ) -> None:
        if base_slippage_bps < 0 or latency_penalty_bps_per_second < 0:
            raise ValueError("paper fill coefficients cannot be negative")
        self.base_slippage_bps = base_slippage_bps
        self.latency_penalty_bps_per_second = latency_penalty_bps_per_second

    def simulate(
        self,
        order: OrderIntent,
        observation: PaperFillObservation,
        *,
        order_id: str,
    ) -> PaperFillSimulation:
        if order.quantity <= 0:
            raise ValueError("quantity must be positive")

        if observation.rejected:
            return PaperFillSimulation(
                ExecutionResult(order_id, "REJECTED", 0, order.reference_price),
                order.quantity,
                0,
                observation.available_quantity,
                observation.queue_ahead_quantity,
                observation.latency_ms,
                observation.reject_reason,
            )

        fillable = max(
            0,
            min(
                order.quantity,
                observation.available_quantity - observation.queue_ahead_quantity,
            ),
        )
        if fillable == 0:
            return PaperFillSimulation(
                ExecutionResult(order_id, "SUBMITTED", 0, order.reference_price),
                order.quantity,
                0,
                observation.available_quantity,
                observation.queue_ahead_quantity,
                observation.latency_ms,
            )

        latency_seconds = Decimal(observation.latency_ms) / Decimal(1000)
        slippage_bps = (
            self.base_slippage_bps
            + latency_seconds * self.latency_penalty_bps_per_second
        )
        direction = Decimal(1) if order.side is Side.BUY else Decimal(-1)
        price = order.reference_price * (
            Decimal(1) + direction * slippage_bps / Decimal(10000)
        )
        status = "FILLED" if fillable == order.quantity else "PARTIALLY_FILLED"
        return PaperFillSimulation(
            ExecutionResult(order_id, status, fillable, price),
            order.quantity,
            fillable,
            observation.available_quantity,
            observation.queue_ahead_quantity,
            observation.latency_ms,
        )
