"""Reconstruct paper account state from fills in one SQLite read snapshot.

Internal accounting consistency only: this is not external broker or market-fill
reconciliation. It never repairs data and cannot detect coordinated tampering of
all source records. Fees are replayed from recorded cash-debit cost rows. Futures
margin is replayed from each fill's immutable margin change; current broker rates
are deliberately not consulted because they may have changed since the fill.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

FUTURES_ASSET_CLASSES = frozenset({"FUTURE", "COMMODITY", "METAL"})


def reconcile_paper(db: sqlite3.Connection, tenant: str) -> dict:
    issues: list[dict] = []

    def issue(code: str, key: str = "account") -> None:
        issues.append({"code": code, "key": key})

    def number(value) -> Decimal:
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError("nonfinite value")
        return result

    db.execute("SAVEPOINT paper_reconciliation")
    try:
        account = db.execute("SELECT * FROM paper_accounts WHERE tenant_id=?", (tenant,)).fetchone()
        fills = db.execute("SELECT * FROM paper_ledger WHERE tenant_id=? ORDER BY id", (tenant,)).fetchall()
        costs = db.execute("SELECT * FROM paper_cost_ledger WHERE tenant_id=? ORDER BY id", (tenant,)).fetchall()
        held = db.execute("SELECT * FROM paper_positions WHERE tenant_id=?", (tenant,)).fetchall()
        margin_rows = db.execute(
            "SELECT * FROM paper_derivative_margin WHERE tenant_id=?", (tenant,)
        ).fetchall()
    finally:
        db.execute("RELEASE SAVEPOINT paper_reconciliation")
    positions: dict[tuple, dict] = {}
    expected_margin: dict[tuple, Decimal] = {}
    cash = Decimal(0)
    observed_cash = None
    try:
        if account is None:
            issue("account_missing")
        else:
            cash = number(account["starting_capital"])
            observed_cash = number(account["cash_balance"])
            if cash <= 0:
                issue("invalid_starting_capital")
        filled_ids = set()
        for fill in fills:
            key = (fill["symbol"], fill["market"], fill["asset_class"])
            order_id = fill["order_id"]
            if fill["status"] != "FILLED":
                issue("unsupported_order_status", order_id)
                continue
            filled_ids.add(order_id)
            quantity = fill["quantity"]
            price = number(fill["fill_price"])
            notional = number(fill["notional"])
            if type(quantity) is not int or quantity <= 0 or price <= 0 or notional != price * quantity:
                issue("invalid_fill_geometry", order_id)
                continue
            state = positions.setdefault(
                key,
                {
                    "quantity": 0,
                    "average_price": Decimal(0),
                    "stop_price": None,
                    "take_profit_price": None,
                },
            )
            derivative = fill["asset_class"] in FUTURES_ASSET_CLASSES
            raw_margin_change = fill["margin_change"]
            margin_change = None if raw_margin_change is None else number(raw_margin_change)
            if derivative and not str(fill["margin_provenance"] or "").strip():
                issue("derivative_margin_provenance_missing", order_id)
            if not derivative and margin_change not in (None, Decimal(0)):
                issue("unexpected_margin_change", order_id)
            if fill["side"] == "BUY":
                new_quantity = state["quantity"] + quantity
                state["average_price"] = (
                    state["average_price"] * state["quantity"] + notional
                ) / new_quantity
                state["quantity"] = new_quantity
                for level in ("stop_price", "take_profit_price"):
                    if fill[level] is not None:
                        state[level] = number(fill[level])
                if derivative:
                    if margin_change is None or margin_change <= 0:
                        issue("invalid_derivative_margin_change", order_id)
                    else:
                        expected_margin[key] = expected_margin.get(key, Decimal(0)) + margin_change
                        cash -= margin_change
                else:
                    cash -= notional
            elif fill["side"] == "SELL" and quantity <= state["quantity"]:
                if derivative:
                    if margin_change is None or margin_change >= 0:
                        issue("invalid_derivative_margin_change", order_id)
                    else:
                        release = -margin_change
                        reserved = expected_margin.get(key, Decimal(0))
                        if release > reserved:
                            issue("derivative_margin_release_exceeds_reserve", order_id)
                        expected_margin[key] = reserved - release
                        cash += release + (price - state["average_price"]) * quantity
                else:
                    cash += notional
                state["quantity"] -= quantity
                if state["quantity"] == 0:
                    del positions[key]
            else:
                issue("invalid_side_or_uncovered_sale", order_id)
        for cost in costs:
            amount = number(cost["amount"])
            if cost["order_id"] not in filled_ids:
                issue("orphan_cost", str(cost["id"]))
            if amount < 0 or cost["cash_debit"] not in (0, 1):
                issue("invalid_cost", str(cost["id"]))
            elif cost["cash_debit"]:
                cash -= amount
        if observed_cash is not None and abs(observed_cash - cash) > Decimal("0.00000001"):
            issue("cash_mismatch")
        observed = {(r["symbol"], r["market"], r["asset_class"]): r for r in held}
        for key in positions.keys() | observed.keys():
            label = ":".join(key)
            expected, actual = positions.get(key), observed.get(key)
            if expected is None or actual is None:
                issue("position_presence_mismatch", label)
                continue
            if expected["quantity"] != actual["quantity"]:
                issue("position_quantity_mismatch", label)
            if abs(expected["average_price"] - number(actual["average_price"])) > Decimal("0.00000001"):
                issue("position_average_mismatch", label)
            for level in ("stop_price", "take_profit_price"):
                value = None if actual[level] is None else number(actual[level])
                if expected[level] != value:
                    issue("position_protection_mismatch", label + ":" + level)
        observed_margin = {
            (r["symbol"], r["market"], r["asset_class"]): r for r in margin_rows
        }
        nonzero_expected = {key: value for key, value in expected_margin.items() if value != 0}
        for key in nonzero_expected.keys() | observed_margin.keys():
            label = ":".join(key)
            expected = nonzero_expected.get(key)
            actual = observed_margin.get(key)
            if expected is None or actual is None:
                issue("derivative_margin_presence_mismatch", label)
                continue
            if abs(expected - number(actual["reserved_margin"])) > Decimal("0.00000001"):
                issue("derivative_margin_amount_mismatch", label)
            if not str(actual["source_provenance"]).strip():
                issue("derivative_margin_provenance_missing", label)
    except (ValueError, InvalidOperation, TypeError, OverflowError):
        issue("invalid_numeric_record")
    return {
        "status": "mismatch" if issues else "matched",
        "tenantId": tenant,
        "checkedAt": datetime.now(timezone.utc).isoformat(),
        "ledgerId": max((r["id"] for r in fills), default=0),
        "fills": len(fills),
        "costRows": len(costs),
        "marginRows": len(margin_rows),
        "expectedCash": str(cash),
        "observedCash": str(observed_cash) if observed_cash is not None else None,
        "issueCount": len(issues),
        "issues": issues[:50],
        "scope": "internal_paper_ledger; not external_broker_or_price_reconciliation",
    }
