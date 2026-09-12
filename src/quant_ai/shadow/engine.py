from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from quant_ai.domain.models import OrderIntent
from quant_ai.execution.paper_broker import PaperBroker
from quant_ai.marketdata.heartbeat import FeedHeartbeat


@dataclass(frozen=True)
class ShadowResult:
    order_id: str
    hypothetical_fill: Decimal
    live_order_sent: bool = False


class ShadowEngine:
    """Observe live-like decisions while physically preventing live order submission."""

    def __init__(self) -> None:
        self.paper = PaperBroker()

    def evaluate(self, order: OrderIntent, heartbeat: FeedHeartbeat, now: datetime) -> ShadowResult:
        heartbeat.assert_fresh(now)
        fill = self.paper.submit(order)
        return ShadowResult(fill.order_id, fill.average_price, False)
