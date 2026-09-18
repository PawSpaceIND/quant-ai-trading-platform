from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from quant_ai.domain.models import OrderIntent
from quant_ai.instruments.identity import instrument_identity_sha256


@dataclass(frozen=True)
class IdempotencyKey:
    value: str


def order_idempotency_key(order: OrderIntent, nonce: str) -> IdempotencyKey:
    if not nonce:
        raise ValueError("nonce is required")
    instrument = getattr(order, "instrument", None)
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
    # Legacy cash keys are a durable replay boundary: preserve their exact bytes.
    if instrument is not None:
        raw += "|instrument:" + instrument_identity_sha256(instrument)
    return IdempotencyKey(sha256(raw.encode()).hexdigest())


class IdempotencyRegistry:
    def __init__(self) -> None:
        self._seen: set[str] = set()

    def claim(self, key: IdempotencyKey) -> bool:
        if key.value in self._seen:
            return False
        self._seen.add(key.value)
        return True
