"""Bounded, read-only external Kite account observation; never the paper ledger."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable

from quant_ai.execution.broker_reads import (
    BrokerReadError,
    identifier,
    kite_data,
    kite_funds,
    kite_positions,
)

IST = timezone(timedelta(hours=5, minutes=30))
SCOPE = "selected_external_kite_equity_net_positions_and_funds_excludes_depository_holdings"


def _normalized(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _normalized(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalized(item) for item in value]
    return value


def capture_external_account(transport: Any, expected_account_id: str, tenant_id: str,
                             *, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> dict:
    expected = identifier(expected_account_id, "expected account ID")
    tenant = identifier(tenant_id, "tenant ID")

    def profile() -> None:
        data = kite_data(transport.get("/user/profile"))
        if not isinstance(data, dict) or data.get("user_id") != expected:
            raise BrokerReadError("Kite profile differs from the selected external account")

    start = clock()
    if start.tzinfo is None or start.utcoffset() is None:
        raise BrokerReadError("Capture clock must be timezone-aware")
    profile()
    funds_before = kite_funds(transport.get("/user/margins"), expected)
    positions_before = kite_positions(transport.get("/portfolio/positions"), expected)
    funds_after = kite_funds(transport.get("/user/margins"), expected)
    positions_after = kite_positions(transport.get("/portfolio/positions"), expected)
    profile()
    end = clock()
    if end.tzinfo is None or end.utcoffset() is None or not 0 <= (end - start).total_seconds() <= 30 or start.astimezone(IST).date() != end.astimezone(IST).date():
        raise BrokerReadError("Capture interval is invalid, exceeds 30 seconds or crosses the broker day")
    stable = funds_before == funds_after and positions_before == positions_after
    complete = funds_after.cash_balance is not None and funds_after.available_balance is not None
    before = {"funds": _normalized(asdict(funds_before)), "positions": _normalized([asdict(p) for p in positions_before])}
    after = {"funds": _normalized(asdict(funds_after)), "positions": _normalized([asdict(p) for p in positions_after])}
    for view in (before, after):
        view["funds"].pop("account_id")
        for item in view["positions"]:
            item.pop("account_id")
    payload = {"schema": "pramana.external_account_snapshot.v1", "scope": SCOPE, "broker": "zerodha-kite", "tenantId": tenant,
               "accountRef": hashlib.sha256(f"zerodha-kite:{expected}".encode()).hexdigest(),
               "startedAt": start.astimezone(timezone.utc).isoformat(timespec="milliseconds"),
               "finishedAt": end.astimezone(timezone.utc).isoformat(timespec="milliseconds"),
               "status": "changing" if not stable else "incomplete" if not complete else "consistent",
               "fundsBefore": before["funds"], "positionsBefore": before["positions"],
               "funds": after["funds"], "positions": after["positions"],
               "limitations": ["Kite net positions exclude a separate depository holdings inventory.",
                               "This external account is not the independent paper ledger; no cash or position parity is claimed.",
                               "Sequential GETs are not atomic and do not prove provider completeness or settlement."]}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    if len(canonical) > 8_000_000:
        raise BrokerReadError("Selected account snapshot exceeds 8 MB")
    return {**payload, "sha256": hashlib.sha256(canonical).hexdigest()}
