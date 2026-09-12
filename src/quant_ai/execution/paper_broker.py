from decimal import Decimal
from itertools import count

from quant_ai.brokers.base import Broker, ExecutionResult
from quant_ai.domain.models import OrderIntent


class PaperBroker(Broker):
    """Deterministic paper executor. It never connects to a real broker."""

    def __init__(self, slippage_bps: Decimal = Decimal(1)) -> None:
        self.slippage_bps = slippage_bps
        self._ids = count(1)

    def submit(self, order: OrderIntent) -> ExecutionResult:
        if order.quantity <= 0:
            raise ValueError("quantity must be positive")

        direction = Decimal(1) if order.side.value == "BUY" else Decimal(-1)
        slip = order.reference_price * (self.slippage_bps / Decimal(10000)) * direction
        fill_price = order.reference_price + slip

        return ExecutionResult(
            order_id=f"PAPER-{next(self._ids):08d}",
            status="FILLED",
            filled_quantity=order.quantity,
            average_price=fill_price,
        )
