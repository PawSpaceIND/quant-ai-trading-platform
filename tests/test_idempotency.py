from decimal import Decimal

from quant_ai.domain.models import Market, OrderIntent, Side
from quant_ai.operations.idempotency import IdempotencyRegistry, order_idempotency_key


def test_duplicate_order_key_is_rejected() -> None:
    order = OrderIntent("AAPL", Market.USA, Side.BUY, 1, Decimal(100), "s1")
    key = order_idempotency_key(order, "bar-1")
    registry = IdempotencyRegistry()
    assert registry.claim(key)
    assert not registry.claim(key)
