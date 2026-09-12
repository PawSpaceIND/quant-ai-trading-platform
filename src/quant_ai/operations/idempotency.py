from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from quant_ai.domain.models import OrderIntent


@dataclass(frozen=True)
class IdempotencyKey:
    value: str


def order_idempotency_key(order: OrderIntent, nonce: str) -> IdempotencyKey:
    if not nonce:
        raise ValueError("nonce is required")
    raw = "|".join(
        [
            order.tenant_id,
            order.strategy_id,
            order.market.value,
            order.symbol,
            order.side.value,
            str(order.quantity),
            str(order.reference_price),
            nonce,
        ]
    )
    return IdempotencyKey(sha256(raw.encode()).hexdigest())


class IdempotencyRegistry:
    def __init__(self) -> None:
        self._seen: set[str] = set()

    def claim(self, key: IdempotencyKey) -> bool:
        if key.value in self._seen:
            return False
        self._seen.add(key.value)
        return True
