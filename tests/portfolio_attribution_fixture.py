"""Shared Python/Node fixture, generated exclusively from synthetic timeline events."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from quant_ai.research.portfolio_sim import PortfolioJournal
from quant_ai.research.portfolio_workspace import workspace_snapshot


def scenarios(directory: Path, normalize=True):
    config = {
        "candidates": ["active", "cash"], "symbols": ["NSE:TCS", "NSE:INFY", "NSE:BANK"],
        "starting_cash_inr": "10000", "fee_bps": "10", "slippage_bps": "25",
        "max_position_fraction": "1", "max_gross_fraction": "1", "max_drawdown_fraction": ".9",
        "max_quote_age_seconds": 60, "order_ttl_seconds": 20, "max_order_quantity": 20,
    }
    journal = PortfolioJournal(directory / "contribution.sqlite", config)
    snapshots = {}

    def capture(name):
        body = json.loads(workspace_snapshot(journal, "Synthetic contribution drill", "default")["payload"])
        # Volatile export metadata is not part of the shared arithmetic expectation.
        if normalize:
            body["generated_at"] = "2000-01-02T00:00:00+00:00"
            body["implementation"] = {"source_sha256": "a" * 64, "python_version": "fixture"}
        snapshots[name] = body

    def append(second, kind, symbol="NSE:TCS", **fields):
        at = datetime(2000, 1, 1, 4, tzinfo=timezone.utc) + timedelta(seconds=second)
        event = {"id": f"{kind}-{second}", "at": at.isoformat(), "kind": kind}
        if kind == "quote":
            event.update(symbol=symbol, bid="100", ask="102", bid_quantity=100,
                         ask_quantity=100, session_open=True, provenance="SYNTHETIC_PRIVATE_SOURCE")
        if kind == "order":
            event.update(candidate="active", symbol=symbol, quantity=1, side="BUY",
                         decision_ref="SYNTHETIC_PRIVATE_DECISION")
        journal.append({**event, **fields})

    try:
        capture("empty")
        append(0, "quote")
        append(1, "order", quantity=10)
        append(2, "quote", ask_quantity=4)
        append(3, "order", quantity=2)
        append(4, "quote", bid="98", ask="100")
        append(5, "order", side="SELL", quantity=3)
        append(6, "quote", bid="110", ask="112")
        append(7, "quote", "NSE:INFY", bid="50", ask="52")
        append(8, "order", "NSE:INFY", quantity=2)
        append(9, "quote", "NSE:INFY", bid="50", ask="52")
        append(10, "order", "NSE:INFY", side="SELL", quantity=2)
        append(11, "quote", "NSE:INFY", bid="45", ask="47")
        append(12, "quote", "NSE:BANK", bid="80", ask="82")
        append(13, "order", "NSE:BANK")
        append(14, "quote", "NSE:BANK", bid="80", ask="82")
        capture("fresh")
        append(75, "clock")
        capture("stale")
        append(76, "quote", bid="115", ask="117")
        capture("partial")
        append(77, "quote", "NSE:BANK", bid="85", ask="87")
        capture("restored")
        append(78, "order", side="SELL", quantity=3)
        append(79, "quote", bid="120", ask="122")
        append(80, "order")
        append(81, "quote", bid="119", ask="121")
        capture("reopened")
    finally:
        journal.close()
    return snapshots


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as folder:
        destination = Path(__file__).parent / "fixtures" / "portfolio-attribution.json"
        destination.write_text(json.dumps(scenarios(Path(folder)), indent=2) + "\n")
