"""Reconciled paper trade episodes. Fills and completed trades are different counts."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from quant_ai.execution.reconciliation import reconcile_paper


def build_trade_evidence(db: sqlite3.Connection, tenant: str) -> dict:
    db.execute("SAVEPOINT trade_evidence")
    try:
        reconciliation = reconcile_paper(db, tenant)
        fills = [dict(r) for r in db.execute("SELECT * FROM paper_ledger WHERE tenant_id=? ORDER BY id", (tenant,))]
        costs = [dict(r) for r in db.execute("SELECT * FROM paper_cost_ledger WHERE tenant_id=? ORDER BY id", (tenant,))]
        scope_exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='pilot_scope' AND type='table'").fetchone()
        scope = db.execute("SELECT currency,market FROM pilot_scope WHERE tenant_id=?", (tenant,)).fetchone() if scope_exists else None
    finally:
        db.execute("RELEASE SAVEPOINT trade_evidence")
    base = {
        "schema": "pramana.paper_trade_evidence.v1", "tenantId": tenant,
        "generatedAt": datetime.now(timezone.utc).isoformat(), "ledgerId": reconciliation["ledgerId"],
        "fillCount": len(fills), "reconciliationStatus": reconciliation["status"],
        "sourceSha256": hashlib.sha256(json.dumps({"fills": fills, "costs": costs}, sort_keys=True).encode()).hexdigest(),
        "countDefinition": "flat_to_flat_per_instrument",
        "scope": "Account-level paper episodes; not strategy attribution, holdout or verified forward performance",
    }
    if reconciliation["status"] != "matched":
        return {**base, "status": "unavailable", "reason": "paper_account_not_reconciled"}
    if not scope or (scope["currency"], scope["market"]) != ("INR", "INDIA") or any(r["market"] != "INDIA" or r["asset_class"] not in ("EQUITY", "ETF") for r in fills):
        return {**base, "status": "unavailable", "reason": "verified_INR_cash_scope_required"}
    charges: dict[str, Decimal] = {}
    for cost in costs:
        if cost["cash_debit"]:
            charges[cost["order_id"]] = charges.get(cost["order_id"], Decimal(0)) + Decimal(cost["amount"])
    opened: dict[tuple, dict] = {}
    closed = []
    last_times = {}
    try:
        for fill in fills:
            timestamp = datetime.fromisoformat(fill["created_at"])
            if timestamp.utcoffset() is None:
                raise ValueError("Naive fill time")
            key = (fill["market"], fill["asset_class"], fill["symbol"])
            if key in last_times and timestamp < last_times[key]:
                raise ValueError("Out-of-order instrument fills")
            last_times[key] = timestamp
            episode = opened.setdefault(key, {"symbol": fill["symbol"], "market": fill["market"],
                "assetClass": fill["asset_class"], "openedAt": fill["created_at"], "lastAt": timestamp,
                "quantity": 0, "orderIds": [], "grossPnl": Decimal(0), "fees": Decimal(0)})
            if timestamp < episode["lastAt"]:
                raise ValueError("Out-of-order fill time")
            episode["lastAt"] = timestamp
            quantity, notional = fill["quantity"], Decimal(fill["notional"])
            episode["quantity"] += quantity if fill["side"] == "BUY" else -quantity
            episode["grossPnl"] += -notional if fill["side"] == "BUY" else notional
            episode["fees"] += charges.get(fill["order_id"], Decimal(0))
            episode["orderIds"].append(fill["order_id"])
            if episode["quantity"] == 0:
                closed.append({"symbol": episode["symbol"], "market": episode["market"],
                    "assetClass": episode["assetClass"], "openedAt": episode["openedAt"],
                    "closedAt": fill["created_at"], "orderIds": episode["orderIds"],
                    "grossPnl": str(episode["grossPnl"]), "cashFees": str(episode["fees"]),
                    "netPnl": str(episode["grossPnl"] - episode["fees"])})
                del opened[key]
    except (ValueError, TypeError):
        return {**base, "status": "unavailable", "reason": "invalid_fill_timestamps"}
    pnls = [Decimal(e["netPnl"]) for e in closed]
    wins, losses = [p for p in pnls if p > 0], [p for p in pnls if p < 0]
    gains, loss = sum(wins, Decimal(0)), -sum(losses, Decimal(0))
    total = sum(pnls, Decimal(0))
    return {**base, "status": "ok", "currency": "INR", "summary": {
        "completedTrades": len(closed), "openEpisodes": len(opened),
        "wins": len(wins), "losses": len(losses), "breakeven": len(pnls) - len(wins) - len(losses),
        "netPnl": str(total), "expectancy": str(total / len(pnls)) if pnls else None,
        "winRate": str(Decimal(len(wins)) / len(pnls)) if pnls else None,
        "profitFactor": str(gains / loss) if loss > 0 else None,
        "profitFactorState": "defined" if loss > 0 else "no_observed_losses" if wins else "insufficient_data",
        "averageWin": str(gains / len(wins)) if wins else None,
        "averageLoss": str(loss / len(losses)) if losses else None,
        "closedCashFees": str(sum((Decimal(e["cashFees"]) for e in closed), Decimal(0))),
        "openCashFees": str(sum((e["fees"] for e in opened.values()), Decimal(0))),
    }, "episodes": closed, "openPositions": [{"symbol": e["symbol"], "quantity": e["quantity"],
        "openedAt": e["openedAt"], "fillCount": len(e["orderIds"])} for e in opened.values()],
        "limitations": ["Recorded cash fees only; execution spread/slippage are already in fill prices.",
            "Open-episode partial exits and fees are excluded from completed-trade statistics.",
            "No strategy/version linkage, calibrated probability, MTM drawdown, regime validation or infrastructure/AI cost allocation is inferred.",
            "Episodes may be serially dependent; counts and positive historical expectancy do not establish a future edge."]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with closing(sqlite3.connect(f"{args.database.resolve().as_uri()}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        report = build_trade_evidence(db, args.tenant)
    report["reportSha256"] = hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest()
    with args.output.open("x") as out:
        args.output.chmod(0o600)
        json.dump(report, out, indent=2, allow_nan=False)
    print(json.dumps({"status": report["status"], "reportSha256": report["reportSha256"], "output": str(args.output)}))
    return 0 if report["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
