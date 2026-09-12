from __future__ import annotations

from dataclasses import dataclass

from quant_ai.brokers.base import Broker, ExecutionResult
from quant_ai.compliance.gates import ComplianceContext, execution_allowed
from quant_ai.domain.models import OrderIntent
from quant_ai.execution.live_factory import LiveMoneyDisabledError


@dataclass
class BrokerRouter:
    paper_broker: Broker
    live_broker: Broker | None = None

    def submit(self, order: OrderIntent, context: ComplianceContext) -> ExecutionResult:
        if not execution_allowed(context):
            raise PermissionError("execution blocked by compliance gate")
        if context.execution_mode.value == "LIVE":
            raise LiveMoneyDisabledError("live broker routing is intentionally disabled")
        return self.paper_broker.submit(order)
