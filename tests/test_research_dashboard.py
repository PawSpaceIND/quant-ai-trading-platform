import hashlib
import json
from datetime import datetime, timezone

from quant_ai.research.company_events import CompanyEvents
from quant_ai.research.dashboard_export import snapshot, write_snapshot
from quant_ai.research.lab import ResearchLab
from quant_ai.research.portfolio_sim import PortfolioJournal


def test_missing_sources_are_explicit_and_not_created(tmp_path):
    missing = tmp_path / "does-not-exist.db"
    report = snapshot({"comparison": {"database": str(missing), "experiment": "x"}})
    assert not missing.exists()
    assert all(m["status"] == "unavailable" for m in report["modules"])
    assert str(tmp_path) not in json.dumps(report)
    assert report["paperOnly"] is True


def test_wrong_database_does_not_write_tables(tmp_path):
    path = tmp_path / "research.db"
    lab = ResearchLab(path)
    lab.close()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    report = snapshot(
        {"simulation": {"database": str(path)}, "companyEvents": {"database": str(path)}}
    )
    assert all(m["status"] == "unavailable" for m in report["modules"])
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_comparison_keeps_unknown_cost_and_omits_private_payload(tmp_path):
    path = tmp_path / "research.db"
    lab = ResearchLab(path)
    lab.create(
        "exp",
        {
            "candidates": ["model", "cash"],
            "baseline": "cash",
            "mode": "historical",
            "expected_cases": 1,
            "max_quantity": 1,
            "max_quote_age_seconds": 60,
            "capital_per_case": "1000",
            "fee_bps": "10",
            "slippage_bps": "10",
            "protocol_version": "test",
        },
    )
    at = "2026-01-01T00:00:00+00:00"
    lab.add_case(
        "exp",
        "case",
        {
            "decision_at": at,
            "quote_at": at,
            "exchange": "NSE",
            "asset_class": "EQUITY",
            "currency": "INR",
            "symbol": "INFY",
            "reference_price": "100",
            "adjustment_status": "unverified",
            "sources": [
                {"id": "data", "available_at": at, "provenance": "PRIVATE_PAYLOAD_MUST_NOT_LEAK"}
            ],
        },
    )
    digest = lab.export_evidence("exp")["body"]["cases"][0]["input_digest"]
    lab.record_decision(
        "exp",
        "case",
        "model",
        {
            "input_digest": digest,
            "model_version": "test",
            "prompt_version": "test",
            "decided_at": at,
            "status": "provider_error",
            "latency_ms": 0,
            "api_cost_usd": None,
            "input_tokens": 0,
            "output_tokens": 0,
        },
    )
    lab.close()
    report = snapshot({"comparison": {"database": str(path), "experiment": "exp"}})
    module = report["modules"][0]
    assert module["status"] == "incomplete"
    assert module["observedAt"] == at
    metrics = {m["label"]: m["value"] for m in module["rows"][0]["metrics"]}
    assert metrics["Errors"] == "1"
    assert metrics["Decisions with unknown cost"] == "1"
    assert "PRIVATE_PAYLOAD" not in json.dumps(report)


def test_simulation_exports_observation_time_and_null_stale_equity(tmp_path):
    path = tmp_path / "sim.db"
    config = {
        "candidates": ["model"],
        "symbols": ["NSE:INFY"],
        "starting_cash_inr": "1000",
        "fee_bps": "0",
        "slippage_bps": "0",
        "max_position_fraction": "1",
        "max_gross_fraction": "1",
        "max_drawdown_fraction": "0.1",
        "max_quote_age_seconds": 10,
        "order_ttl_seconds": 20,
        "max_order_quantity": 5,
    }
    store = PortfolioJournal(path, config)
    store.append(
        {
            "id": "order",
            "kind": "order",
            "at": "2026-01-01T00:00:00Z",
            "symbol": "NSE:INFY",
            "candidate": "model",
            "side": "BUY",
            "quantity": 5,
            "decision_ref": "PRIVATE_DECISION_REFERENCE",
        }
    )
    store.append(
        {
            "id": "quote",
            "kind": "quote",
            "at": "2026-01-01T00:00:01Z",
            "symbol": "NSE:INFY",
            "bid": "100",
            "ask": "100",
            "bid_quantity": 5,
            "ask_quantity": 5,
            "session_open": True,
            "provenance": "synthetic",
        }
    )
    store.append({"id": "clock", "kind": "clock", "at": "2026-01-01T00:01:00Z"})
    store.close()
    report = snapshot({"simulation": {"database": str(path)}})
    module = report["modules"][1]
    assert module["status"] == "incomplete"
    assert module["observedAt"] == "2026-01-01T00:01:00Z"
    assert "PRIVATE_DECISION_REFERENCE" not in json.dumps(report)
    assert any(
        m == {"label": "Equity (INR)", "value": "Unavailable"} for m in module["rows"][0]["metrics"]
    )


def test_company_events_do_not_claim_empty_feed_success(tmp_path):
    path = tmp_path / "events.db"
    store = CompanyEvents(path)
    store.ingest(b"<rss><channel/></rss>", observed_at="2026-01-01T00:00:00Z")
    store.close()
    module = snapshot({"companyEvents": {"database": str(path)}})["modules"][2]
    assert module["status"] == "incomplete"
    assert any(m["value"] == "Imported" for m in module["rows"][0]["metrics"])


def test_atomic_export_is_private(tmp_path):
    target = tmp_path / "snapshot.json"
    body = snapshot({}, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    write_snapshot(target, body)
    assert json.loads(target.read_text()) == body
    assert target.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".research-*"))
