import copy
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from quant_ai.research.company_events import CompanyEvents, parse_feed
from quant_ai.research.lab import ResearchLab
from quant_ai.research.portfolio_sim import PortfolioJournal, replay
from quant_ai.research.providers import Provider, evaluate_case, usage_cost


def config():
    return {
        "candidates": ["claude", "astra", "cash"],
        "starting_cash_inr": "1000",
        "fee_bps": "0",
        "slippage_bps": "0",
        "max_position_fraction": "1",
        "max_gross_fraction": "1",
        "max_drawdown_fraction": "0.10",
        "max_quote_age_seconds": 30,
        "order_ttl_seconds": 20,
        "max_order_quantity": 20,
        "symbols": ["NSE:INFY", "NSE:TCS"],
    }


def at(second):
    return (datetime(2026, 1, 1, 4, 0, tzinfo=timezone.utc) + timedelta(seconds=second)).isoformat()


def quote(second, *, price="100", qty=20, symbol="NSE:INFY", **changes):
    return {
        "id": "q" + str(second) + symbol,
        "kind": "quote",
        "at": at(second),
        "symbol": symbol,
        "bid": price,
        "ask": price,
        "bid_quantity": qty,
        "ask_quantity": qty,
        "session_open": True,
        "provenance": "synthetic-test",
        **changes,
    }


def order(second, *, side="BUY", qty=5, **changes):
    return {
        "id": "o" + str(second),
        "kind": "order",
        "at": at(second),
        "symbol": "NSE:INFY",
        "candidate": "claude",
        "side": side,
        "quantity": qty,
        "decision_ref": "test-case-digest",
        **changes,
    }


def test_continuous_cash_and_partial_exit_across_restart(tmp_path):
    path = tmp_path / "sim.db"
    journal = PortfolioJournal(path, config())
    events = [quote(0), order(1), quote(2), order(3, side="SELL"), quote(4, price="110", qty=2)]
    for e in events:
        journal.append(e)
    before = journal.report()
    journal.close()
    journal = PortfolioJournal(path, config())
    assert journal.report() == before
    b = before["books"]["claude"]
    assert b["cash_inr"] == "720"
    assert b["positions"]["NSE:INFY"]["quantity"] == 3
    assert b["realized_pnl_inr"] == "20"
    assert b["curve"][-1]["equity_inr"] == "1050"
    assert before["books"]["astra"]["cash_inr"] == "1000"
    assert journal.append(events[-1]) == before
    with pytest.raises(ValueError, match="event_id_conflict"):
        journal.append({**events[-1], "bid": "80"})
    journal.close()


def test_next_quote_not_same_timestamp():
    b = replay(config(), [quote(0), order(1), quote(1), quote(2)])["books"]["claude"]
    assert b["fills"][0]["at"] == at(2)


def test_liquidity_shared_between_same_candidate_orders():
    b = replay(config(), [quote(0), order(1), order(2), quote(3, qty=6)])["books"]["claude"]
    assert [f["quantity"] for f in b["fills"]] == [5, 1]
    assert b["cancelled"][0]["quantity"] == 4


def test_drawdown_blocks_buys_but_allows_exit():
    b = replay(
        config(),
        [
            quote(0),
            order(1, qty=10),
            quote(2),
            order(3),
            quote(4, price="80"),
            order(5, side="SELL", qty=10),
            quote(6, price="80"),
        ],
    )["books"]["claude"]
    assert b["halted"]
    assert [f["side"] for f in b["fills"]] == ["BUY", "SELL"]
    assert b["cash_inr"] == "800"


def test_stale_marks_do_not_look_like_current_equity():
    events = [
        quote(0),
        order(1),
        quote(2),
        {"id": "clock", "kind": "clock", "at": at(33)},
        order(34, symbol="NSE:TCS"),
        quote(35, symbol="NSE:TCS"),
    ]
    b = replay(config(), events)["books"]["claude"]
    assert b["curve"][-1]["equity_inr"] is None
    assert b["curve"][-1]["stale_symbols"] == ["NSE:INFY"]
    assert len(b["fills"]) == 1


@pytest.mark.parametrize(
    "closed,when,reason", [(True, 2, "session_closed"), (False, 22, "expired")]
)
def test_holiday_and_expiry(closed, when, reason):
    b = replay(config(), [quote(0), order(1), quote(when, session_open=not closed)])["books"][
        "claude"
    ]
    assert not b["fills"]
    assert b["cancelled"][0]["reason"] == reason


def test_costs_and_post_fill_position_cap():
    c = {**config(), "fee_bps": "100", "slippage_bps": "100", "max_position_fraction": "0.5"}
    b = replay(c, [quote(0), order(1, qty=20), quote(2)])["books"]["claude"]
    assert b["fills"][0]["quantity"] == 4
    assert Decimal(b["cash_inr"]) == Decimal("591.96")
    assert Decimal(b["fees_inr"]) == Decimal("4.04")
    assert Decimal(b["curve"][-1]["equity_inr"]) == Decimal("991.96")


@pytest.mark.parametrize(
    "events",
    [
        [quote(2), quote(1)],
        [quote(0), quote(0)],
        [quote(0, bid="101", ask="100")],
        [order(0, qty=True)],
        [order(0, qty=21)],
        [order(0, side="SHORT")],
        [quote(0, symbol="MCX:GOLD")],
        [quote(0, session_open="yes")],
    ],
)
def test_invalid_portfolio_events_rejected(events):
    with pytest.raises((ValueError, TypeError)):
        replay(config(), events)


def lab_fixture(tmp_path):
    lab = ResearchLab(tmp_path / "research.db")
    lab.create(
        "exp",
        {
            "candidates": ["claude", "astra", "cash"],
            "baseline": "cash",
            "mode": "historical",
            "expected_cases": 2,
            "max_quantity": 10,
            "max_quote_age_seconds": 60,
            "capital_per_case": "1000",
            "fee_bps": "10",
            "slippage_bps": "10",
            "protocol_version": "test-v1",
        },
    )
    lab.add_case(
        "exp",
        "case",
        {
            "decision_at": at(0),
            "quote_at": at(0),
            "exchange": "NSE",
            "asset_class": "EQUITY",
            "currency": "INR",
            "symbol": "INFY",
            "reference_price": "100",
            "adjustment_status": "unverified",
            "sources": [{"id": "price", "available_at": at(0), "provenance": "synthetic-test"}],
        },
    )
    return lab


def payload():
    return {"action": "HOLD", "quantity": 0, "rationale": "No signal", "evidence_ids": ["price"]}


def raw(provider, decision=None):
    result = {
        "id": "request123",
        "model": "test-model",
        "usage": {"input_tokens": 30, "output_tokens": 10},
    }
    if provider == "openai":
        return {
            **result,
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": json.dumps(decision or payload())}],
                }
            ],
        }
    return {
        **result,
        "stop_reason": "tool_use",
        "content": [{"type": "tool_use", "name": "paper_decision", "input": decision or payload()}],
    }


def provider_pair(handler):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return {
        "claude": Provider("anthropic", "claude-test", "secret", client=client),
        "astra": Provider("openai", "astra-test", "secret", client=client),
    }


def test_real_adapter_shapes_identical_packets_and_persistence(tmp_path):
    prompts, requests = [], []

    def handler(request):
        data = json.loads(request.content)
        is_openai = request.url.host == "api.openai.com"
        prompts.append(
            data["input"][-1]["content"] if is_openai else data["messages"][0]["content"]
        )
        requests.append(data)
        return httpx.Response(200, json=raw("openai" if is_openai else "anthropic"))

    lab = lab_fixture(tmp_path)
    result = evaluate_case(lab, "exp", "case", provider_pair(handler), tmp_path / "receipts")
    assert set(result.values()) == {"ok"}
    assert prompts[0] == prompts[1]
    assert requests[1]["store"] is False
    report = lab.report("exp")
    assert report["candidates"]["astra"]["unknown_cost_decisions"] == 1
    assert report["candidates"]["astra"]["cost_total_complete"] is False
    assert report["candidates"]["cash"]["cost_total_complete"] is True
    assert (
        evaluate_case(lab, "exp", "case", provider_pair(handler), tmp_path / "receipts")["astra"]
        == "already_recorded"
    )
    assert len(prompts) == 2
    lab.close()


def test_provider_errors_not_holds_and_no_secret_output(tmp_path):
    def handler(request):
        return httpx.Response(401, json={"error": "do not log secret"})

    lab = lab_fixture(tmp_path)
    evaluate_case(lab, "exp", "case", provider_pair(handler), tmp_path / "receipts")
    report = lab.report("exp")["candidates"]["astra"]
    assert report["errors"] == 1 and report["holds"] == 0
    assert "do not log secret" not in json.dumps(lab.export_evidence("exp"))
    lab.close()


@pytest.mark.parametrize(
    "decision",
    [
        {**payload(), "quantity": True},
        {**payload(), "evidence_ids": ["future"]},
        {**payload(), "action": "BUY", "quantity": 99},
        {**payload(), "extra": "bad"},
    ],
)
def test_model_schema_failure_persisted(tmp_path, decision):
    def handler(request):
        return httpx.Response(
            200,
            json=raw("openai" if request.url.host == "api.openai.com" else "anthropic", decision),
        )

    lab = lab_fixture(tmp_path)
    result = evaluate_case(lab, "exp", "case", provider_pair(handler), tmp_path / "receipts")
    assert result["astra"] == "invalid"
    assert lab.report("exp")["candidates"]["claude"]["errors"] == 1
    lab.close()


def test_refusal_and_truncation():
    p = Provider("openai", "test")
    with pytest.raises(ValueError, match="incomplete"):
        p.parse({"status": "incomplete"})
    with pytest.raises(ValueError, match="refusal"):
        p.parse(
            {
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "refusal"}]}],
            }
        )


def test_costs_unknown_and_cache_accounted():
    rates = {"input_per_million": "10", "output_per_million": "50", "cached_input_per_million": "1"}
    r = {
        "usage": {
            "input_tokens": 100,
            "output_tokens": 10,
            "input_tokens_details": {"cached_tokens": 50},
        }
    }
    assert usage_cost("openai", r, rates) == (100, 10, "0.00105", True)
    assert usage_cost("openai", r, None)[2] is None
    assert usage_cost("openai", {}, rates)[3] is False


def test_receipt_survives_database_write_failure_no_paid_retry(tmp_path, monkeypatch):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json=raw("anthropic"))

    lab = lab_fixture(tmp_path)

    def crash(*args):
        raise RuntimeError("simulated database outage")

    monkeypatch.setattr(lab, "record_decision", crash)
    with pytest.raises(RuntimeError):
        evaluate_case(lab, "exp", "case", provider_pair(handler), tmp_path / "receipts")
    with pytest.raises(FileExistsError):
        evaluate_case(lab, "exp", "case", provider_pair(handler), tmp_path / "receipts")
    assert len(calls) == 1
    assert (
        json.loads(next((tmp_path / "receipts").glob("*.json")).read_text())["state"]
        == "response_recorded"
    )
    lab.close()


def feed(description="Original", pub="Thu, 01 Jan 2026 04:00:00 +0000"):
    return (
        "<rss><channel><item><title>Infosys Limited</title><link>https://nsearchives.nseindia.com/a.pdf</link>"
        "<guid>one</guid><pubDate>"
        + pub
        + "</pubDate><description>"
        + description
        + "</description></item></channel></rss>"
    ).encode()


def test_event_first_seen_mapping_and_revision_cutoffs(tmp_path):
    e = CompanyEvents(tmp_path / "events.db")
    e.ingest(feed(), observed_at=at(10))
    assert e.sources_as_of("NSE:INFY", at(11)) == []
    e.map_company("Infosys Limited", "NSE:INFY", at(12), "reviewed instrument master")
    assert e.sources_as_of("NSE:INFY", at(11)) == []
    original = e.sources_as_of("NSE:INFY", at(12))
    assert original[0]["available_at"] == at(12)
    e.ingest(feed(), observed_at=at(15))
    assert e.sources_as_of("NSE:INFY", at(16)) == original
    e.ingest(feed("Correction"), observed_at=at(20))
    assert e.sources_as_of("NSE:INFY", at(19)) == original
    assert e.sources_as_of("NSE:INFY", at(20))[0]["data"]["description"] == "Correction"
    assert e.status()["revisions"] == 2
    e.close()


@pytest.mark.parametrize(
    "raw_xml", [b"<!DOCTYPE rss><rss/>", b"<html>denied</html>", b"x" * 5_000_001]
)
def test_bad_feed_rejected(raw_xml):
    with pytest.raises(ValueError):
        parse_feed(raw_xml, at(10))


def test_future_and_nonofficial_feed_items_quarantined():
    items, rejected = parse_feed(feed(pub="Thu, 01 Jan 2026 05:00:00 +0000"), at(10))
    assert not items and rejected[0]["reason"] == "future_publication"
    items, rejected = parse_feed(
        feed().replace(b"https://nsearchives.nseindia.com", b"https://evil.example"), at(10)
    )
    assert not items and rejected[0]["reason"] == "non_official_attachment_link"


def test_database_separation_and_config_freeze(tmp_path):
    lab = lab_fixture(tmp_path)
    with pytest.raises(ValueError, match="not_a_simulation"):
        PortfolioJournal(tmp_path / "research.db", config())
    with pytest.raises(ValueError, match="not_an_event"):
        CompanyEvents(tmp_path / "research.db")
    lab.close()
    journal = PortfolioJournal(tmp_path / "sim.db", config())
    journal.close()
    changed = copy.deepcopy(config())
    changed["starting_cash_inr"] = "2000"
    with pytest.raises(ValueError, match="config_changed"):
        PortfolioJournal(tmp_path / "sim.db", changed)


def test_provider_manifest_mismatch_prevents_network(tmp_path):
    lab = lab_fixture(tmp_path)
    packet = lab.export_evidence("exp")["body"]["cases"][0]["packet"]
    config2 = lab.config("exp")
    config2["providers"] = {"astra": {"provider": "openai", "model": "frozen-model"}}
    lab.create("frozen", config2)
    lab.add_case("frozen", "case", packet)

    def handler(request):
        pytest.fail("network must not be called")

    with pytest.raises(ValueError, match="manifest_mismatch"):
        evaluate_case(lab, "frozen", "case", provider_pair(handler), tmp_path / "receipts")
    lab.close()


def test_event_content_tamper_detected(tmp_path):
    store = CompanyEvents(tmp_path / "events.db")
    store.ingest(feed(), observed_at=at(10))
    body = json.loads(store.db.execute("SELECT body FROM event_revisions").fetchone()[0])
    body["description"] = "tampered"
    with store.db:
        store.db.execute("UPDATE event_revisions SET body=?", (json.dumps(body),))
    with pytest.raises(ValueError, match="event_integrity"):
        store.sources_as_of("NSE:INFY", at(20))
    store.close()


def test_invalid_append_is_atomic(tmp_path):
    journal = PortfolioJournal(tmp_path / "sim.db", config())
    journal.append(quote(0))
    before = journal.report()
    with pytest.raises(ValueError):
        journal.append(quote(1, bid="200", ask="100"))
    assert journal.report() == before
    assert len(journal.events()) == 1
    journal.close()


def test_same_quote_cost_drawdown_halts_subsequent_orders():
    cfg = {**config(), "fee_bps": "500", "max_drawdown_fraction": "0.01"}
    book = replay(cfg, [quote(0), order(1), order(2), quote(3)])["books"]["claude"]
    assert len(book["fills"]) == 1
    assert book["halted"]


def test_feed_failure_is_visible_and_does_not_delete_history(tmp_path, monkeypatch):
    store = CompanyEvents(tmp_path / "events.db")
    store.ingest(feed(), observed_at=at(10))

    def fail():
        raise httpx.ReadTimeout("sensitive internal message")

    monkeypatch.setattr(store, "_fetch", fail)
    with pytest.raises(httpx.ReadTimeout):
        store.fetch()
    status = store.status()
    assert status["last_capture"]["status"] == "error"
    assert status["last_capture"]["error_type"] == "ReadTimeout"
    assert status["revisions"] == 1
    assert "sensitive" not in json.dumps(status)
    store.close()


def test_simulation_cli_round_trip(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    data = tmp_path / "timeline.json"
    data.write_text(
        json.dumps(
            {
                "config": config(),
                "events": [
                    quote(0),
                    order(1),
                    quote(2),
                    order(3, side="SELL"),
                    quote(4, price="110"),
                ],
            }
        )
    )
    command = [
        sys.executable,
        str(root / "scripts/research_extensions.py"),
        "simulate",
        str(tmp_path / "simulation.db"),
        "--file",
        str(data),
    ]
    env = dict(os.environ, PYTHONPATH=str(root / "src"))
    first = subprocess.run(command, env=env, capture_output=True, text=True, check=True)
    second = subprocess.run(command, env=env, capture_output=True, text=True, check=True)
    assert json.loads(first.stdout) == json.loads(second.stdout)
    assert json.loads(first.stdout)["books"]["claude"]["cash_inr"] == "1050"
