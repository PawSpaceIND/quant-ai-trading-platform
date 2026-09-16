"""Account/day-scoped external execution IDs; never infer identity from trade economics."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import date, datetime
from zoneinfo import ZoneInfo

SCHEMA = "pramana.broker_execution_identity.v1"
_FIELDS = {"schema", "broker", "tenantId", "accountRef", "tradingDay", "exchange", "tradeId", "brokerOrderId"}


def execution_identity(value: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise TypeError("external_execution_identity_must_be_mapping")
    if set(value) != _FIELDS:
        raise ValueError("external_execution_identity_fields_invalid")
    result = dict(value)
    if any(not isinstance(item, str) or not item.strip() or len(item) > 160
           or any(ord(char) < 32 for char in item) for item in result.values()):
        raise ValueError("external_execution_identity_value_invalid")
    if result["schema"] != SCHEMA or result["broker"] != "zerodha-kite":
        raise ValueError("external_execution_identity_provider_invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", result["accountRef"]):
        raise ValueError("external_execution_account_invalid")
    if date.fromisoformat(result["tradingDay"]).isoformat() != result["tradingDay"]:
        raise ValueError("external_execution_day_invalid")
    return result


def external_fill_id(value: Mapping[str, str]) -> str:
    raw = json.dumps(execution_identity(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "KITE2:" + hashlib.sha256(raw.encode()).hexdigest()


def assert_fill_source(
    source: Mapping[str, str], *, tenant_id: str, broker_order_id: str | None,
    at: datetime, binding: Mapping[str, str] | None,
) -> None:
    """Check economic attribution in addition to the source identity digest."""
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("external_fill_time_must_be_timezone_aware")
    if source["tradingDay"] != at.astimezone(ZoneInfo("Asia/Kolkata")).date().isoformat():
        raise ValueError("external_fill_trading_day_mismatch")
    if source["tenantId"] != tenant_id or source["brokerOrderId"] != broker_order_id:
        raise ValueError("external_fill_order_scope_mismatch")
    if binding is None:
        raise ValueError("external_fill_broker_binding_required")
    expected = {"broker": "broker", "accountRef": "account_ref",
                "brokerOrderId": "broker_order_id", "exchange": "exchange"}
    if any(source[key] != binding.get(field) for key, field in expected.items()):
        raise ValueError("external_fill_broker_scope_mismatch")
