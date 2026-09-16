"""Canonical approved-order snapshot, including optional immutable contract identity."""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

from quant_ai.domain.models import AssetClass, InstrumentBoundOrderIntent, Market, OrderIntent, Side
from quant_ai.instruments.identity import canonical_instrument_identity, instrument_from_identity


def bound_identity(order: OrderIntent) -> str | None:
    instrument = getattr(order, "instrument", None)
    if instrument is None:
        return None
    # Reconstruct to validate symbol/market/class and freeze a defensive metadata copy.
    values = {key: getattr(order, key) for key in OrderIntent.__dataclass_fields__}
    checked = InstrumentBoundOrderIntent(**values, instrument=instrument)
    return canonical_instrument_identity(checked.instrument)


def canonical_order_intent(order: OrderIntent) -> str:
    for name in ("tenant_id", "strategy_id", "symbol"):
        value = getattr(order, name)
        if (not isinstance(value, str) or not value or value != value.strip()
                or len(value) > 256 or any(ord(char) < 32 for char in value)):
            raise ValueError("order_snapshot_identity_invalid")
    if type(order.quantity) is not int or not 0 < order.quantity <= 2**53 - 1:
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


def order_from_snapshot(raw: str) -> OrderIntent:
    """Recover exactly what was approved; missing legacy snapshots are not inferred."""
    if not isinstance(raw, str) or len(raw.encode()) > 65536:
        raise ValueError("order_snapshot_payload_invalid")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("order_snapshot_duplicate_key")
            result[key] = value
        return result
    try:
        payload = json.loads(raw, object_pairs_hook=unique)
        expected = {"schema", "tenantId", "strategyId", "symbol", "market", "assetClass",
                    "side", "quantity", "referencePrice", "stopPrice", "takeProfitPrice", "instrumentIdentity"}
        if not isinstance(payload, dict) or set(payload) != expected:
            raise ValueError("order_snapshot_fields_invalid")
        if payload["schema"] != "pramana.order_intent_snapshot.v1":
            raise ValueError("order_snapshot_schema_invalid")
        for name in ("referencePrice", "stopPrice", "takeProfitPrice"):
            value = payload[name]
            if value is not None and (not isinstance(value, str) or len(value) > 256):
                raise ValueError("order_snapshot_money_invalid")
        values = {
            "symbol": payload["symbol"], "market": Market(payload["market"]), "side": Side(payload["side"]),
            "quantity": payload["quantity"], "reference_price": Decimal(payload["referencePrice"]),
            "strategy_id": payload["strategyId"], "asset_class": AssetClass(payload["assetClass"]),
            "tenant_id": payload["tenantId"],
            "stop_price": None if payload["stopPrice"] is None else Decimal(payload["stopPrice"]),
            "take_profit_price": None if payload["takeProfitPrice"] is None else Decimal(payload["takeProfitPrice"]),
        }
        identity = payload["instrumentIdentity"]
        if identity is None:
            result = OrderIntent(**values)
        else:
            result = InstrumentBoundOrderIntent(**values, instrument=instrument_from_identity(identity))
        if canonical_order_intent(result) != raw:
            raise ValueError("order_snapshot_noncanonical")
        return result
    except (KeyError, TypeError, InvalidOperation, json.JSONDecodeError) as error:
        raise ValueError("order_snapshot_payload_invalid") from error
