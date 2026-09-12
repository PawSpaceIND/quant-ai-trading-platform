from __future__ import annotations

from dataclasses import dataclass

from quant_ai.brokers.base import Broker, ExecutionResult
from quant_ai.compliance.gates import ComplianceContext, execution_allowed
from quant_ai.domain.models import OrderIntent


@dataclass
class BrokerRouter:
    paper_broker: Broker
    live_broker: Broker | None = None

    def submit(self, order: OrderIntent, context: ComplianceContext) -> ExecutionResult:
        if not execution_allowed(context):
            raise PermissionError("execution blocked by compliance gate")
        if context.execution_mode.value == "LIVE":
            if self.live_broker is None:
                raise RuntimeError("live broker is not configured")
            return self.live_broker.submit(order)
        return self.paper_broker.submit(order)
