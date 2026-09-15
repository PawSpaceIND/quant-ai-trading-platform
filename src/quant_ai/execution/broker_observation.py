"""Bounded GET-only Kite order/trade observations of an explicitly selected account.

Repeated observations test agreement, not atomicity or economic correctness. No
paper ledger, order submission, credential, user profile, or inferred equity is
published. The report is evidence for inspection, never a promotion gate.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable
from zoneinfo import ZoneInfo

from quant_ai.execution.broker_reads import BrokerReadError, amount, identifier, integer, kite_data

IST = ZoneInfo("Asia/Kolkata")
TERMINAL = {"COMPLETE", "CANCELLED", "REJECTED"}
STATUSES = TERMINAL | {"OPEN", "TRIGGER PENDING", "VALIDATION PENDING", "PUT ORDER REQ RECEIVED",
    "OPEN PENDING", "MODIFY VALIDATION PENDING", "MODIFY PENDING", "CANCEL PENDING", "AMO REQ RECEIVED"}
SCOPE = "kite_daily_order_trade_consistency_only"


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def _price(value: Any, field: str, *, positive: bool = False) -> str:
    number = amount(value, field)
    if number < 0 or number > Decimal(1000000000000) or positive and number == 0 or number != number.quantize(Decimal("0.00000001")):
        raise BrokerReadError("Broker price is out of supported bounds")
    return format(number.quantize(Decimal("0.00000001")), "f")


def _time(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise BrokerReadError(f"Missing broker {field}")
    try:
        observed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
    except ValueError as error:
        raise BrokerReadError(f"Invalid broker {field}") from error
    return observed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _rows(payload: Any, *, trades: bool) -> list[dict]:
    rows = kite_data(payload)
    if not isinstance(rows, list) or len(rows) > (10000 if trades else 5000):
        raise BrokerReadError("Kite order/trade collection is missing or exceeds the capture limit")
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise BrokerReadError("Invalid Kite order/trade row")
        common = {
            "orderId": identifier(row.get("order_id"), "order ID"),
            "instrumentId": str(integer(row.get("instrument_token"), "instrument token", positive=True)),
            "symbol": identifier(row.get("tradingsymbol"), "symbol"),
            "exchange": identifier(row.get("exchange"), "exchange"),
            "product": identifier(row.get("product"), "product"),
            "side": identifier(row.get("transaction_type"), "side"),
            "quantity": integer(row.get("quantity"), "quantity", positive=True),
        }
        if common["side"] not in {"BUY", "SELL"}:
            raise BrokerReadError("Unknown broker side")
        if trades:
            common.update(tradeId=identifier(row.get("trade_id"), "trade ID"),
                exchangeOrderId=identifier(row.get("exchange_order_id"), "exchange order ID"),
                price=_price(row.get("average_price"), "trade price", positive=True),
                at=_time(row.get("fill_timestamp"), "fill timestamp"))
            key = (common["exchange"], common["tradeId"])
        else:
            common.update(status=identifier(row.get("status"), "status"),
                variety=identifier(row.get("variety"), "variety"),
                filled=integer(row.get("filled_quantity"), "filled quantity"),
                pending=integer(row.get("pending_quantity"), "pending quantity"),
                cancelled=integer(row.get("cancelled_quantity"), "cancelled quantity"),
                averagePrice=_price(row.get("average_price"), "order average price"),
                exchangeOrderId=identifier(row["exchange_order_id"], "exchange order ID") if row.get("exchange_order_id") else None,
                at=_time(row.get("order_timestamp"), "order timestamp"))
            if any(common[name] < 0 for name in ("filled", "pending", "cancelled")):
                raise BrokerReadError("Negative order quantity component")
            key = common["orderId"]
        if key in seen:
            raise BrokerReadError("Duplicate broker trade/order identity")
        seen.add(key)
        result.append(common)
    return sorted(result, key=lambda r: (r["exchange"], r.get("tradeId", ""), r["orderId"]))


def inspect_capture(capture: dict) -> dict:
    """Inspect normalized evidence. The independent dashboard reader repeats these checks."""
    stable = capture["ordersBefore"] == capture["orders"] and capture["tradesBefore"] == capture["trades"]
    issues = []
    def issue(code, order_id=None):
        issues.append({"code": code, "orderId": order_id})
    by_order = {row["orderId"]: row for row in capture["orders"]}
    fills = {}
    day = datetime.fromisoformat(capture["finishedAt"]).astimezone(IST).date()
    end = datetime.fromisoformat(capture["finishedAt"])
    for trade in capture["trades"]:
        order = by_order.get(trade["orderId"])
        if not order:
            issue("orphan_trade", trade["orderId"])
        elif any(trade[k] != order[k] for k in ("instrumentId", "symbol", "exchange", "product", "side", "exchangeOrderId")):
            issue("trade_identity_mismatch", trade["orderId"])
        at = datetime.fromisoformat(trade["at"])
        if at.astimezone(IST).date() != day or at > end:
            issue("trade_time_outside_capture_day", trade["orderId"])
        if order and at < datetime.fromisoformat(order["at"]):
            issue("trade_precedes_order", trade["orderId"])
        fills.setdefault(trade["orderId"], []).append(trade)
    for order in capture["orders"]:
        order_id = order["orderId"]
        linked = fills.get(order_id, [])
        qty = sum(t["quantity"] for t in linked)
        if datetime.fromisoformat(order["at"]) > end:
            issue("order_time_after_capture", order_id)
        if order["status"] not in STATUSES:
            issue("unsupported_status", order_id)
        if order["variety"] not in {"regular", "amo"}:
            issue("unsupported_order_variety", order_id)
        if order["filled"] != qty:
            issue("filled_quantity_mismatch", order_id)
        if max(order["pending"], order["filled"] + order["cancelled"]) > order["quantity"]:
            issue("quantity_components_exceed_order", order_id)
        if order["status"] == "COMPLETE":
            if order["filled"] != order["quantity"] or order["pending"] or order["cancelled"]:
                issue("complete_order_quantities", order_id)
            if qty:
                total = sum(int(t["price"].replace(".", "")) * t["quantity"] for t in linked)
                deviation = total - int(order["averagePrice"].replace(".", "")) * qty
                if abs(deviation) > 1_000_000 * qty:
                    issue("complete_average_price_mismatch", order_id)
        if order["status"] == "REJECTED" and order["filled"]:
            issue("rejected_order_has_fills", order_id)
    return {"status": "changing" if not stable else "issues" if issues else "consistent" if by_order else "empty",
        "issueCount": len(issues), "issues": issues[:50], "orderCount": len(by_order),
        "tradeCount": len(capture["trades"]), "openOrderCount": sum(o["status"] not in TERMINAL for o in by_order.values())}


def capture_kite(transport: Any, expected_account_id: str, tenant_id: str, *,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> dict:
    expected = identifier(expected_account_id, "expected account ID")
    tenant = identifier(tenant_id, "tenant ID")
    def identity():
        data = kite_data(transport.get("/user/profile"))
        if not isinstance(data, dict) or data.get("user_id") != expected:
            raise BrokerReadError("Kite profile differs from the selected external account")
    start = clock()
    if start.tzinfo is None:
        raise BrokerReadError("Capture clock must be timezone-aware")
    identity()
    before_orders = _rows(transport.get("/orders"), trades=False)
    before_trades = _rows(transport.get("/trades"), trades=True)
    orders = _rows(transport.get("/orders"), trades=False)
    trades = _rows(transport.get("/trades"), trades=True)
    identity()
    end = clock()
    if end.tzinfo is None or not 0 <= (end-start).total_seconds() <= 30 or start.astimezone(IST).date() != end.astimezone(IST).date():
        raise BrokerReadError("Capture interval is invalid, exceeds 30 seconds or crosses the broker day")
    capture = {"schema": "pramana.broker_observation.v1", "scope": SCOPE, "tenantId": tenant,
        "broker": "zerodha-kite", "accountRef": hashlib.sha256(f"zerodha-kite:{expected}".encode()).hexdigest(),
        "startedAt": start.astimezone(timezone.utc).isoformat(timespec="milliseconds"),
        "finishedAt": end.astimezone(timezone.utc).isoformat(timespec="milliseconds"),
        "ordersBefore": before_orders, "tradesBefore": before_trades, "orders": orders, "trades": trades}
    if len(canonical(capture)) > 7_999_000:
        raise BrokerReadError("Normalized broker capture exceeds 8 MB")
    return {**capture, "sha256": digest(capture)}
