"""Actual stored research/event fixtures for deployment wiring; no external calls."""
import hashlib
import json
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from quant_ai.research.company_events import CompanyEvents
from quant_ai.research.dashboard_export import snapshot, write_snapshot
from quant_ai.research.lab import ResearchLab
from quant_ai.research.portfolio_sim import PortfolioJournal
from quant_ai.research.portfolio_workspace import publish as publish_portfolio
from quant_ai.research.workspace_report import publish as publish_comparison


def at(second):
    return (datetime(2026, 1, 1, 4, tzinfo=timezone.utc) + timedelta(seconds=second)).isoformat()


def build(directory: Path, tenant: str):
    directory.mkdir(mode=0o700)
    with closing(ResearchLab(directory / "research.sqlite")) as lab:
        lab.create("deployment-fixture", {
            "candidates": ["active", "cash"], "baseline": "cash", "mode": "historical",
            "expected_cases": 2, "max_quantity": 10, "max_quote_age_seconds": 60,
            "capital_per_case": "1000", "fee_bps": "10", "slippage_bps": "10",
            "protocol_version": "synthetic-deployment-verification",
        })
        lab.add_case("deployment-fixture", "missing-decisions", {
            "decision_at": at(0), "quote_at": at(0), "exchange": "NSE", "asset_class": "EQUITY",
            "currency": "INR", "symbol": "INFY", "reference_price": "100",
            "adjustment_status": "unverified",
            "sources": [{"id":"price", "available_at":at(0), "provenance":"SYNTHETIC_DEPLOYMENT_FIXTURE"}],
        })
    config = {"candidates":["active","cash"], "symbols":["NSE:INFY"], "starting_cash_inr":"1000",
              "fee_bps":"0", "slippage_bps":"0", "max_position_fraction":"1", "max_gross_fraction":"1",
              "max_drawdown_fraction":"0.10", "max_quote_age_seconds":30, "order_ttl_seconds":20,
              "max_order_quantity":20}
    with closing(PortfolioJournal(directory / "portfolio.sqlite", config)) as journal:
        for second in range(5):
            event = {"id":str(second), "at":at(second), "symbol":"NSE:INFY"}
            if second % 2 == 0:
                price, quantity = ("110",2) if second == 4 else ("100",20)
                event.update(kind="quote", bid=price, ask=price, bid_quantity=quantity,
                             ask_quantity=quantity, session_open=True, provenance="SYNTHETIC_DEPLOYMENT_FIXTURE")
            else:
                event.update(kind="order", candidate="active", side="BUY" if second == 1 else "SELL",
                             quantity=5, decision_ref="SYNTHETIC_DEPLOYMENT_FIXTURE")
            journal.append(event)
    with closing(CompanyEvents(directory / "events.sqlite")) as events:
        raw = ("<rss><channel><item><title>Infosys Limited</title>"
               "<link>https://nsearchives.nseindia.com/synthetic-fixture.pdf</link><guid>fixture</guid>"
               "<pubDate>Thu, 01 Jan 2026 04:00:00 +0000</pubDate>"
               "<description>Synthetic deployment disclosure</description></item></channel></rss>")
        events.ingest(raw.encode(), observed_at=at(10))
        events.map_company("Infosys Limited", "NSE:INFY", at(11), "SYNTHETIC_MAPPING_NOT_QUALIFIED")
    comparison, portfolio = directory / "comparison-001.json", directory / "portfolio-001.json"
    publish_comparison(directory / "research.sqlite", "deployment-fixture", tenant, comparison)
    publish_portfolio(directory / "portfolio.sqlite", "Synthetic deployment replay", tenant, portfolio)
    write_snapshot(directory / "dashboard-snapshot.json", snapshot({
        "comparison": {"database": str(directory / "research.sqlite"), "experiment": "deployment-fixture"},
        "simulation": {"database": str(directory / "portfolio.sqlite")},
        "companyEvents": {"database": str(directory / "events.sqlite")},
    }, now=datetime(2026, 1, 1, 4, 0, 20, tzinfo=timezone.utc)))
    receipt = {"tenant":tenant, "source":"synthetic", "networkRequests":0,
               "comparisonSha256":hashlib.sha256(comparison.read_bytes()).hexdigest(),
               "portfolioSha256":hashlib.sha256(portfolio.read_bytes()).hexdigest()}
    (directory / "fixture-receipt.json").write_text(json.dumps(receipt))
    return receipt
