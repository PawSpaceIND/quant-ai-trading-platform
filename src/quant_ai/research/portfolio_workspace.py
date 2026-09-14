"""Sanitized continuous-portfolio evidence for the authenticated research workspace."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from quant_ai.research.lab import canonical, identity, instant
from quant_ai.research.portfolio_sim import PortfolioJournal, replay

MAX_BYTES = 4_000_000
CONFIG_FIELDS = (
    "starting_cash_inr",
    "fee_bps",
    "slippage_bps",
    "max_position_fraction",
    "max_gross_fraction",
    "max_drawdown_fraction",
    "max_quote_age_seconds",
    "order_ttl_seconds",
    "max_order_quantity",
)
LIMITATIONS = [
    "Replayed supplied events, not a live portfolio or independently verified forward record.",
    "Missing valuations remain gaps; observed maximum drawdown may understate loss across gaps.",
    "Each candidate has separate starting cash on the shared supplied quote timeline.",
    "Session status, instrument metadata, prices, adjustments and liquidity need qualification.",
    "Later-quote IOC fills are simulated; no queue priority, market impact or corporate-action processing.",
    "Trading fees are included; model, infrastructure and data charges are excluded.",
    "Drawdown halts block new buys in this simulation; losses can exceed configured thresholds.",
    "No winner, strategy acceptance or trading permission is produced by this report.",
]


def implementation():
    folder = Path(__file__).resolve().parent
    files = {
        name: hashlib.sha256((folder / name).read_bytes()).hexdigest()
        for name in ("lab.py", "portfolio_sim.py", "portfolio_workspace.py")
    }
    return {
        "source_sha256": hashlib.sha256(canonical(files).encode()).hexdigest(),
        "python_version": sys.version.split()[0],
    }


def workspace_snapshot(journal: PortfolioJournal, name: str, tenant: str) -> dict:
    identity(name)
    identity(tenant)
    code = implementation()
    evidence = journal.export_evidence()
    config, events = evidence["body"]["config"], evidence["body"]["events"]
    if len(config["candidates"]) > 16 or len(config["symbols"]) > 50 or len(events) > 5000:
        raise ValueError("portfolio_workspace_too_large")
    result = replay(config, events)
    as_of = events[-1]["at"] if events else None
    quotes = {e["symbol"]: e for e in events if e["kind"] == "quote"}
    orders = {e["id"]: e for e in events if e["kind"] == "order"}
    initial = Decimal(str(config["starting_cash_inr"]))
    books = {}
    for candidate, book in result["books"].items():
        curve = [
            {
                "event_index": i,
                "at": p["at"],
                "equity_inr": p["equity_inr"],
                "drawdown_fraction": p["drawdown_fraction"],
                "stale_symbols": p["stale_symbols"],
            }
            for i, p in enumerate(book["curve"])
        ]
        latest = curve[-1] if curve else None
        current = (
            Decimal(latest["equity_inr"]) if latest and latest["equity_inr"] is not None else None
        )
        drawdowns = [
            Decimal(p["drawdown_fraction"]) for p in curve if p["drawdown_fraction"] is not None
        ]
        holdings = []
        for symbol, position in book["positions"].items():
            if position["quantity"] == 0:
                continue
            quote = quotes.get(symbol)
            fresh = bool(
                quote
                and as_of
                and 0
                <= (instant(as_of) - instant(quote["at"])).total_seconds()
                <= config["max_quote_age_seconds"]
            )
            value = Decimal(str(quote["bid"])) * position["quantity"] if fresh else None
            holdings.append(
                {
                    "symbol": symbol,
                    "quantity": position["quantity"],
                    "cost_inr": position["cost_inr"],
                    "mark_fresh": fresh,
                    "last_bid": str(quote["bid"]) if quote else None,
                    "quote_at": quote["at"] if quote else None,
                    "market_value_inr": str(value) if value is not None else None,
                    "unrealized_pnl_inr": str(value - Decimal(position["cost_inr"]))
                    if value is not None
                    else None,
                }
            )
        remaining_cost = sum((Decimal(h["cost_inr"]) for h in holdings), Decimal(0))
        fills = [
            {
                k: f[k]
                for k in (
                    "order_id",
                    "quote_id",
                    "symbol",
                    "side",
                    "quantity",
                    "price",
                    "fee_inr",
                    "at",
                )
            }
            for f in book["fills"]
        ]
        pending = [
            {
                "order_id": o["id"],
                "symbol": o["symbol"],
                "side": o["side"],
                "quantity": o["quantity"],
                "submitted_at": o["at"],
            }
            for o in book["orders"]
        ]
        cancelled = [
            {
                "order_id": c["id"],
                "symbol": orders[c["id"]]["symbol"],
                "side": orders[c["id"]]["side"],
                "reason": c["reason"],
                "quantity": c.get("quantity", orders[c["id"]]["quantity"]),
            }
            for c in book["cancelled"]
        ]
        books[candidate] = {
            "cash_inr": book["cash_inr"],
            "current_equity_inr": str(current) if current is not None else None,
            "net_return_fraction": str(current / initial - 1) if current is not None else None,
            "realized_pnl_inr": book["realized_pnl_inr"],
            "unrealized_pnl_inr": str(current - Decimal(book["cash_inr"]) - remaining_cost)
            if current is not None
            else None,
            "fees_inr": book["fees_inr"],
            "peak_observed_equity_inr": book["peak_equity_inr"],
            "current_drawdown_fraction": latest["drawdown_fraction"] if latest else None,
            "max_observed_drawdown_fraction": str(max(drawdowns)) if drawdowns else None,
            "unvalued_observations": sum(p["equity_inr"] is None for p in curve),
            "halted": book["halted"],
            "stale_symbols": latest["stale_symbols"] if latest else [],
            "holdings": holdings,
            "fills": fills,
            "pending_orders": pending,
            "cancelled_orders": cancelled,
            "curve": curve,
        }
    body = {
        "schema": "pramana.portfolio_workspace.v1",
        "tenant_id": tenant,
        "name": name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "as_of": as_of,
        "evidence_sha256": evidence["sha256"],
        "implementation": code,
        "mode": "research_simulation",
        "status": "insufficient_evidence",
        "automatic_promotion": False,
        "event_count": len(events),
        "quote_events": sum(e["kind"] == "quote" for e in events),
        "order_events": len(orders),
        "symbols": list(config["symbols"]),
        "config": {
            key: str(config[key])
            if key not in ("max_quote_age_seconds", "order_ttl_seconds", "max_order_quantity")
            else config[key]
            for key in CONFIG_FIELDS
        },
        "books": books,
        "limitations": LIMITATIONS,
    }
    if implementation() != code:
        raise ValueError("simulation_source_changed_during_export")
    payload = canonical(body)
    envelope = {"payload": payload, "sha256": hashlib.sha256(payload.encode()).hexdigest()}
    if len(canonical(envelope).encode()) > MAX_BYTES:
        raise ValueError("portfolio_workspace_too_large")
    return envelope


def publish(database: Path, name: str, tenant: str, output: Path) -> dict:
    journal = PortfolioJournal(database, readonly=True)
    try:
        envelope = workspace_snapshot(journal, name, tenant)
    finally:
        journal.close()
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(envelope, stream, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
    return envelope


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--name", required=True)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    publish(args.database, args.name, args.tenant, args.output)


if __name__ == "__main__":
    main()
