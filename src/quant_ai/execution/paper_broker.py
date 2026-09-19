from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from itertools import count

from quant_ai.brokers.base import Broker, ExecutionResult
from quant_ai.domain.models import OrderIntent
from quant_ai.execution.paper_fill_lifecycle import (
    PaperFillLifecycleSimulator,
    PaperFillObservation,
    PaperFillSimulation,
)


class PaperBroker(Broker):
    """Deterministic paper executor. It never connects to a real broker."""

    def __init__(
        self,
        slippage_bps: Decimal = Decimal(1),
        *,
        lifecycle: PaperFillLifecycleSimulator | None = None,
        observation_provider: Callable[[OrderIntent], PaperFillObservation] | None = None,
    ) -> None:
        if (lifecycle is None) != (observation_provider is None):
            raise ValueError("paper fill lifecycle and observation provider must be paired")
        self.slippage_bps = slippage_bps
        self.lifecycle = lifecycle
        self.observation_provider = observation_provider
        self.last_simulation: PaperFillSimulation | None = None
        self._ids = count(1)

    def submit(self, order: OrderIntent) -> ExecutionResult:
        if order.quantity <= 0:
            raise ValueError("quantity must be positive")

        order_id = f"PAPER-{next(self._ids):08d}"
        if self.lifecycle is not None and self.observation_provider is not None:
            self.last_simulation = self.lifecycle.simulate(
                order,
                self.observation_provider(order),
                order_id=order_id,
            )
            return self.last_simulation.result

        direction = Decimal(1) if order.side.value == "BUY" else Decimal(-1)
        slip = order.reference_price * (self.slippage_bps / Decimal(10000)) * direction
        fill_price = order.reference_price + slip
        self.last_simulation = None
        return ExecutionResult(
            order_id=order_id,
            status="FILLED",
            filled_quantity=order.quantity,
            average_price=fill_price,
        )
