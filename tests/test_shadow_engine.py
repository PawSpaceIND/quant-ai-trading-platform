from datetime import datetime, timezone
from decimal import Decimal

from quant_ai.domain.models import Market, OrderIntent, Side
from quant_ai.marketdata.heartbeat import FeedHeartbeat
from quant_ai.shadow.engine import ShadowEngine


def test_shadow_never_sends_live_order() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    order = OrderIntent("AAPL", Market.USA, Side.BUY, 1, Decimal(100), "test", stop_price=Decimal(95))
    result = ShadowEngine().evaluate(order, FeedHeartbeat(now), now)
    assert result.live_order_sent is False
    assert result.order_id.startswith("PAPER-")
