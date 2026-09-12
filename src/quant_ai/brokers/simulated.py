from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from quant_ai.brokers.base import Broker, ExecutionResult
from quant_ai.domain.models import OrderIntent


class SimulatedBrokerMode(str, Enum):
    FILL = "FILL"
    REJECT = "REJECT"
    TIMEOUT = "TIMEOUT"
    PARTIAL_FILL = "PARTIAL_FILL"


@dataclass
class SimulatedBroker(Broker):
    mode: SimulatedBrokerMode = SimulatedBrokerMode.FILL

    def submit(self, order: OrderIntent) -> ExecutionResult:
        if self.mode == SimulatedBrokerMode.TIMEOUT:
            raise TimeoutError("simulated_broker_timeout")
        if self.mode == SimulatedBrokerMode.REJECT:
            return ExecutionResult("SIM-REJECT", "REJECTED", 0, Decimal(0))
        if self.mode == SimulatedBrokerMode.PARTIAL_FILL:
            filled = max(1, order.quantity // 2)
            return ExecutionResult("SIM-PARTIAL", "PARTIAL", filled, order.reference_price)
        return ExecutionResult("SIM-FILL", "FILLED", order.quantity, order.reference_price)
