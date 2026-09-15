import hashlib
import json
import sqlite3
import stat
from decimal import Decimal

import pytest

from quant_ai.research.portfolio_sim import PortfolioJournal, validate_config
from quant_ai.research.portfolio_workspace import publish, workspace_snapshot


def config():
    return {
        "candidates": ["active", "cash"],
        "symbols": ["NSE:TEST"],
        "starting_cash_inr": "1000",
        "fee_bps": "100",
        "slippage_bps": "0",
        "max_position_fraction": "1",
        "max_gross_fraction": "1",
        "max_drawdown_fraction": ".1",
        "max_quote_age_seconds": 60,
        "order_ttl_seconds": 20,
        "max_order_quantity": 20,
        "private_setting": "PRIVATE_CONFIG_SENTINEL",
    }


def event(second, kind, **changes):
    from datetime import datetime, timedelta, timezone

    at = (datetime(2026, 1, 1, 4, tzinfo=timezone.utc) + timedelta(seconds=second)).isoformat()
    body = {"id": f"{kind}-{second}", "kind": kind, "at": at}
    if kind == "quote":
        body.update(
            symbol="NSE:TEST",
            bid="100",
            ask="101",
            bid_quantity=100,
            ask_quantity=100,
            session_open=True,
            provenance="PRIVATE_QUOTE_SENTINEL",
        )
    if kind == "order":
        body.update(
            candidate="active",
            symbol="NSE:TEST",
            side="BUY",
            quantity=1,
            decision_ref="PRIVATE_DECISION_SENTINEL",
        )
    return {**body, **changes}


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "journal.sqlite"
    journal = PortfolioJournal(path, config())
    for e in [
        event(0, "quote"),
        event(1, "order", quantity=10),
        event(2, "quote", ask_quantity=4),
        event(3, "order", side="SELL"),
        event(4, "quote", bid="110", ask="111"),
        event(5, "order"),
        event(130, "clock"),
        event(131, "order", side="SELL"),
    ]:
        journal.append(e)
    yield journal, path
    journal.close()


def test_readonly_evidence_does_not_modify_or_create_a_journal(source, tmp_path):
    writer, path = source
    before = path.read_bytes()
    reader = PortfolioJournal(path, readonly=True)
    try:
        assert reader.report() == writer.report()
        assert reader.export_evidence() == writer.export_evidence()
        with pytest.raises(ValueError, match="readonly"):
            reader.append(event(132, "clock"))
        with pytest.raises(sqlite3.OperationalError):
            reader.db.execute("DELETE FROM simulation_events")
    finally:
        reader.close()
    assert path.read_bytes() == before
    missing = tmp_path / "missing.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        PortfolioJournal(missing, readonly=True)
    assert not missing.exists()


def test_partial_fills_fees_and_stale_valuation_are_preserved(source, tmp_path):
    journal, path = source
    output = tmp_path / "workspace.json"
    envelope = publish(path, "Synthetic timeline", "default", output)
    report = json.loads(envelope["payload"])
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert "PRIVATE_" not in output.read_text()
    assert envelope["sha256"] == hashlib.sha256(envelope["payload"].encode()).hexdigest()
    assert report["evidence_sha256"] == journal.export_evidence()["sha256"]
    b = report["books"]["active"]
    # Independent cash calculation: buy 4 at 101 + 4.04 fees, sell 1 at 110 - 1.10 fees.
    assert Decimal(b["cash_inr"]) == Decimal("700.86")
    assert Decimal(b["realized_pnl_inr"]) == Decimal("6.89")
    assert Decimal(b["fees_inr"]) == Decimal("5.14")
    assert b["current_equity_inr"] is None and b["net_return_fraction"] is None
    assert b["unrealized_pnl_inr"] is None and b["current_drawdown_fraction"] is None
    assert b["unvalued_observations"] == 2 and b["stale_symbols"] == ["NSE:TEST"]
    assert [f["quantity"] for f in b["fills"]] == [4, 1]
    assert b["holdings"][0]["quantity"] == 3
    assert Decimal(b["holdings"][0]["cost_inr"]) == Decimal("306.03")
    assert b["holdings"][0]["market_value_inr"] is None
    assert [(c["reason"], c["quantity"]) for c in b["cancelled_orders"]] == [
        ("liquidity_or_risk_cap", 6),
        ("expired", 1),
    ]
    assert b["pending_orders"][0]["side"] == "SELL"
    assert b["curve"][-2]["equity_inr"] is None and b["curve"][-1]["equity_inr"] is None
    assert Decimal(b["max_observed_drawdown_fraction"]) == Decimal(".00804")
    assert report["books"]["cash"]["current_equity_inr"] == "1000"
    assert report["automatic_promotion"] is False
    assert report["status"] == "insufficient_evidence"


def test_fresh_gap_restores_values_but_preserves_history_and_halts_buys(source):
    journal, _ = source
    journal.append(event(132, "quote", bid="50", ask="51"))
    journal.append(event(133, "order"))
    journal.append(event(134, "quote", bid="50", ask="51"))
    b = json.loads(workspace_snapshot(journal, "Gap", "default")["payload"])["books"]["active"]
    assert b["halted"] is True and b["unvalued_observations"] == 2
    assert Decimal(b["current_equity_inr"]) == Decimal("850.36")
    assert Decimal(b["realized_pnl_inr"]) == Decimal("-45.62")
    assert Decimal(b["unrealized_pnl_inr"]) == Decimal("-104.02")
    assert Decimal(b["net_return_fraction"]) == Decimal("-.14964")
    assert b["cancelled_orders"][-1]["reason"] == "halted_or_stale"
    assert len(b["fills"]) == 3  # Exit proceeded; the later buy did not.


@pytest.mark.parametrize("target", ["digest", "identity", "sequence", "config"])
def test_inconsistent_journal_fails_before_report_publication(source, tmp_path, target):
    journal, _ = source
    with journal.db:
        if target == "digest":
            journal.db.execute("UPDATE simulation_events SET digest='bad' WHERE seq=0")
        elif target == "identity":
            journal.db.execute("UPDATE simulation_events SET id='wrong' WHERE seq=0")
        elif target == "sequence":
            journal.db.execute("UPDATE simulation_events SET seq=99 WHERE seq=0")
        else:
            journal.db.execute("UPDATE simulation_config SET body='{}'")
    with pytest.raises(ValueError):
        workspace_snapshot(journal, "bad", "default")


def test_report_cannot_overwrite_source_or_existing_destination(source, tmp_path):
    _, path = source
    destination = tmp_path / "existing.json"
    destination.write_text("keep")
    for output in [path, destination]:
        before = output.read_bytes()
        with pytest.raises(FileExistsError):
            publish(path, "review", "default", output)
        assert output.read_bytes() == before
    wrong = tmp_path / "paper.sqlite"
    db = sqlite3.connect(wrong)
    db.execute("CREATE TABLE paper_accounts(cash TEXT)")
    db.close()
    with pytest.raises(ValueError, match="not_a_simulation_database"):
        publish(wrong, "review", "default", tmp_path / "never.json")
    assert not (tmp_path / "never.json").exists()


def test_empty_journal_has_no_observed_performance(tmp_path):
    journal = PortfolioJournal(tmp_path / "empty.sqlite", config())
    try:
        report = json.loads(workspace_snapshot(journal, "empty", "default")["payload"])
        assert report["as_of"] is None and report["event_count"] == 0
        assert all(
            b["current_equity_inr"] is None
            and b["max_observed_drawdown_fraction"] is None
            and b["curve"] == []
            for b in report["books"].values()
        )
    finally:
        journal.close()


@pytest.mark.parametrize("field", ["candidates", "symbols"])
def test_identifier_lists_cannot_be_strings(field):
    broken = config()
    broken[field] = broken[field][0]
    with pytest.raises(ValueError):
        validate_config(broken)
