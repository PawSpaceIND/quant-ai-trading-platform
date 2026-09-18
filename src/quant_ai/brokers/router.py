from __future__ import annotations

from dataclasses import dataclass

from quant_ai.brokers.base import Broker, ExecutionResult
from quant_ai.compliance.gates import ComplianceContext, ExecutionMode, execution_allowed
from quant_ai.domain.models import OrderIntent
from quant_ai.execution.live_factory import LiveMoneyDisabledError


@dataclass
class BrokerRouter:
    paper_broker: Broker
    live_broker: Broker | None = None

    def submit(self, order: OrderIntent, context: ComplianceContext) -> ExecutionResult:
        if not execution_allowed(context):
            raise PermissionError("execution blocked by compliance gate")
        # Preserve the existing instrument-bound subclass; this is a market/asset
        # approval check, not a replacement for its exact contract or risk gates.
        if (not isinstance(order, OrderIntent) or order.market is not context.market
                or order.asset_class is not context.asset_class):
            raise PermissionError("order scope does not match compliance context")
        if context.execution_mode is ExecutionMode.LIVE:
            raise LiveMoneyDisabledError("live broker routing is intentionally disabled")
        return self.paper_broker.submit(order)
