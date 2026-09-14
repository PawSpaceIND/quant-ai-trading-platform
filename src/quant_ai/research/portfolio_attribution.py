"""Inception-to-date cash-flow contribution for the long-only INR research replay.

Execution reference is the fill quote midpoint; terminal holdings remain bid marked.
No external flows, benchmark effects, interest, dividends or corporate actions exist
in this simulator. This is an accounting decomposition, not a counterfactual strategy.
"""

from decimal import Decimal

ZERO = Decimal(0)
FIELDS = (
    "reference_pnl_inr", "spread_cost_inr", "slippage_cost_inr", "fees_inr",
    "realized_pnl_inr", "unrealized_pnl_inr", "net_pnl_inr", "contribution_fraction",
)


def close(actual, expected):
    if actual is None or expected is None:
        return actual is expected
    actual, expected = Decimal(str(actual)), Decimal(str(expected))
    return abs(actual - expected) <= max(Decimal(1), abs(actual), abs(expected)) * Decimal("1e-24")


def contribution(book: dict, initial: Decimal) -> dict:
    """Reconstruct and reconcile fills, fee-inclusive average cost and final marks."""
    states = {}
    for fill in book["fills"]:
        s = states.setdefault(fill["symbol"], {
            "quantity": 0, "cost": ZERO, "cash_flow": ZERO, "reference_flow": ZERO,
            "realized": ZERO, "fees": ZERO, "spread": ZERO, "slippage": ZERO,
        })
        qty, price, fee = fill["quantity"], Decimal(fill["price"]), Decimal(fill["fee_inr"])
        bid, ask = Decimal(fill["quote_bid"]), Decimal(fill["quote_ask"])
        midpoint, buy = (bid + ask) / 2, fill["side"] == "BUY"
        direction = -1 if buy else 1
        if buy:
            s["quantity"] += qty
            s["cost"] += qty * price + fee
        else:
            if qty > s["quantity"]:
                raise ValueError("portfolio_accounting_mismatch")
            cost = s["cost"] * qty / s["quantity"]
            s["cost"] -= cost
            s["quantity"] -= qty
            s["realized"] += qty * price - fee - cost
        s["cash_flow"] += direction * qty * price - fee
        s["reference_flow"] += direction * qty * midpoint
        s["fees"] += fee
        s["spread"] += qty * (ask - bid) / 2
        s["slippage"] += qty * (price - ask if buy else bid - price)

    holdings = {h["symbol"]: h for h in book["holdings"]}
    if set(holdings) != {symbol for symbol, s in states.items() if s["quantity"]}:
        raise ValueError("portfolio_accounting_mismatch")
    rows = []
    for symbol, s in sorted(states.items()):
        h = holdings.get(symbol)
        if h and (h["quantity"] != s["quantity"] or not close(h["cost_inr"], s["cost"])):
            raise ValueError("portfolio_accounting_mismatch")
        value = Decimal(h["market_value_inr"]) if h and h["market_value_inr"] is not None else (None if h else ZERO)
        net = s["cash_flow"] + value if value is not None else None
        unrealized = value - s["cost"] if value is not None else None
        if h and not close(h["unrealized_pnl_inr"], unrealized):
            raise ValueError("portfolio_accounting_mismatch")
        values = {
            "reference_pnl_inr": s["reference_flow"] + value if value is not None else None,
            "spread_cost_inr": s["spread"], "slippage_cost_inr": s["slippage"],
            "fees_inr": s["fees"], "realized_pnl_inr": s["realized"],
            "unrealized_pnl_inr": unrealized, "net_pnl_inr": net,
            "contribution_fraction": net / initial if net is not None else None,
        }
        rows.append({"symbol": symbol, "quantity": s["quantity"],
                     **{k: str(v) if v is not None else None for k, v in values.items()}})

    complete = book["current_equity_inr"] is not None
    totals = {}
    for key in FIELDS:
        available = complete or key in ("spread_cost_inr", "slippage_cost_inr", "fees_inr", "realized_pnl_inr")
        totals[key] = str(sum((Decimal(r[key]) for r in rows), ZERO)) if available else None
    expected = {
        "cash_inr": initial + sum((s["cash_flow"] for s in states.values()), ZERO),
        "realized_pnl_inr": totals["realized_pnl_inr"], "fees_inr": totals["fees_inr"],
        "unrealized_pnl_inr": totals["unrealized_pnl_inr"],
        "net_return_fraction": totals["contribution_fraction"],
    }
    if any(not close(book[key], value) for key, value in expected.items()):
        raise ValueError("portfolio_accounting_mismatch")
    difference = Decimal(totals["net_pnl_inr"]) - (Decimal(book["current_equity_inr"]) - initial) if complete else None
    if difference is not None and not close(Decimal(totals["net_pnl_inr"]), Decimal(book["current_equity_inr"]) - initial):
        raise ValueError("portfolio_accounting_mismatch")
    return {"method": "inception_cash_flow_midpoint_v1", "status": "complete" if complete else "incomplete",
            "rows": rows, "totals": totals,
            "reconciliation_difference_inr": str(difference) if difference is not None else None}
