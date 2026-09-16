"""Canonical approved-order snapshot, including optional immutable contract identity."""
from __future__ import annotations

import json
from decimal import Decimal

from quant_ai.domain.models import InstrumentBoundOrderIntent, OrderIntent
from quant_ai.instruments.identity import canonical_instrument_identity


def bound_identity(order: OrderIntent) -> str | None:
    instrument = getattr(order, "instrument", None)
    if instrument is None:
        return None
    # Reconstruct to validate symbol/market/class and freeze a defensive metadata copy.
    values = {key: getattr(order, key) for key in OrderIntent.__dataclass_fields__}
    checked = InstrumentBoundOrderIntent(**values, instrument=instrument)
    return canonical_instrument_identity(checked.instrument)


def canonical_order_intent(order: OrderIntent) -> str:
    if type(order.quantity) is not int or order.quantity <= 0:
        raise ValueError("order_snapshot_quantity_invalid")
    def money(value, positive=False):
        if value is None and not positive:
            return None
        if not isinstance(value, Decimal) or not value.is_finite() or positive and value <= 0:
            raise ValueError("order_snapshot_money_invalid")
        return str(value)
    payload = {
        "schema": "pramana.order_intent_snapshot.v1", "tenantId": order.tenant_id,
        "strategyId": order.strategy_id, "symbol": order.symbol,
        "market": order.market.value, "assetClass": order.asset_class.value,
        "side": order.side.value, "quantity": order.quantity,
        "referencePrice": money(order.reference_price, True),
        "stopPrice": money(order.stop_price), "takeProfitPrice": money(order.take_profit_price),
        "instrumentIdentity": bound_identity(order),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
