import copy
import hashlib
import sqlite3

import pytest

from quant_ai.research.lab import ResearchLab, canonical


@pytest.fixture
def setup(tmp_path):
    lab = ResearchLab(tmp_path / "research.sqlite")
    config = {
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
    }
    packet = {
        "decision_at": "2026-09-11T10:00:00+05:30",
        "quote_at": "2026-09-11T09:59:50+05:30",
        "exchange": "NSE",
        "asset_class": "EQUITY",
        "currency": "INR",
        "symbol": "TEST",
        "reference_price": "100",
        "adjustment_status": "unverified",
        "sources": [
            {
                "id": "price",
                "available_at": "2026-09-11T09:59:50+05:30",
                "provenance": "synthetic-test-only",
            }
        ],
    }
    lab.create("test", config)
    lab.add_case("test", "one", packet)
    decision = {
        "input_digest": hashlib.sha256(canonical(packet).encode()).hexdigest(),
        "model_version": "synthetic",
        "prompt_version": "v1",
        "decided_at": "2026-09-11T10:00:01+05:30",
        "status": "ok",
        "action": "BUY",
        "quantity": 10,
        "rationale": "test",
        "evidence_ids": ["price"],
        "latency_ms": 100,
        "api_cost_usd": "0.01",
        "input_tokens": 10,
        "output_tokens": 5,
    }
    outcome = {
        "entry_at": "2026-09-11T10:00:02+05:30",
        "exit_at": "2026-09-11T10:01:00+05:30",
        "entry_ask": "100",
        "exit_bid": "110",
        "entry_available_quantity": 20,
        "exit_available_quantity": 20,
        "provenance": "synthetic-test-only",
    }
    yield lab, config, packet, decision, outcome
    lab.close()


def record_all(lab, decision):
    for candidate in ("claude", "astra", "cash"):
        body = copy.deepcopy(decision)
        if candidate == "cash":
            body.update(action="HOLD", quantity=0)
        lab.record_decision("test", "one", candidate, body)


def test_persisted_paired_cash_capped_round_trip(setup):
    lab, _, _, decision, outcome = setup
    record_all(lab, decision)
    lab.record_outcome("test", "one", outcome)
    report = lab.report("test")
    a, b = (report["candidates"][c] for c in ("claude", "astra"))
    assert a == b
    assert a["completed_episodes"] == 1
    assert a["outcomes"][0]["filled_quantity"] == 9
    assert a["completed_case_pnl_inr"] == "86.220090"
    assert report["candidates"]["cash"]["holds"] == 1
    assert report["unverified_adjustments"] == 1
    assert report["status"] == "insufficient_evidence"
    assert report["automatic_promotion"] is False
    path = lab.db.execute("PRAGMA database_list").fetchone()[2]
    other = ResearchLab(path)
    try:
        assert other.report("test") == report
    finally:
        other.close()


@pytest.mark.parametrize(
    "mutation,error",
    [
        (lambda p: p.update(quote_at="2026-09-11T09:00:00+05:30"), "stale_or_future"),
        (lambda p: p.update(decision_at="2026-09-11T10:00:00"), "timezone"),
        (lambda p: p.update(reference_price="NaN"), "invalid_number"),
        (lambda p: p.update(exchange="MCX"), "unsupported"),
        (lambda p: p["sources"][0].update(available_at="2026-09-11T10:01:00+05:30"), "future"),
    ],
)
def test_bad_input_rejected(setup, mutation, error):
    lab, _, packet, _, _ = setup
    mutation(packet)
    with pytest.raises(ValueError, match=error):
        lab.add_case("test", "bad", packet)


@pytest.mark.parametrize(
    "mutation,error",
    [
        (lambda d: d.update(input_digest="different"), "input_mismatch"),
        (lambda d: d.update(quantity=11), "quantity_limit"),
        (lambda d: d.update(quantity=True), "invalid_integer"),
        (lambda d: d.update(evidence_ids=["invented"]), "unknown_evidence"),
        (lambda d: d.update(api_cost_usd="Infinity"), "invalid_number"),
    ],
)
def test_bad_decisions_rejected(setup, mutation, error):
    lab, _, _, decision, _ = setup
    mutation(decision)
    with pytest.raises(ValueError, match=error):
        lab.record_decision("test", "one", "astra", decision)


def test_ordering_and_duplicate_evidence(setup):
    lab, _, _, decision, outcome = setup
    with pytest.raises(ValueError, match="all_candidates"):
        lab.record_outcome("test", "one", outcome)
    record_all(lab, decision)
    with pytest.raises(sqlite3.IntegrityError):
        lab.record_decision("test", "one", "astra", decision)
    bad = dict(outcome, entry_at=decision["decided_at"])
    with pytest.raises(ValueError, match="execution_must_follow"):
        lab.record_outcome("test", "one", bad)
    lab.record_outcome("test", "one", outcome)
    with pytest.raises(ValueError, match="outcome_already_known"):
        lab.record_decision("test", "one", "astra", decision)


@pytest.mark.parametrize(
    "entry_qty,exit_qty,status",
    [(0, 10, "unfilled"), (3, 10, "completed"), (10, 0, "unresolved_exit")],
)
def test_liquidity_is_not_assumed(setup, entry_qty, exit_qty, status):
    lab, _, _, decision, outcome = setup
    record_all(lab, decision)
    outcome.update(entry_available_quantity=entry_qty, exit_available_quantity=exit_qty)
    lab.record_outcome("test", "one", outcome)
    result = lab.report("test")["candidates"]["astra"]["outcomes"][0]
    assert result["status"] == status
    if status != "completed":
        assert result["net_pnl_inr"] is None


def test_provider_failure_counted_and_other_candidate_not_blocked(setup):
    lab, _, _, decision, outcome = setup
    for candidate in ("claude", "astra", "cash"):
        lab.record_decision(
            "test",
            "one",
            candidate,
            dict(decision, status="provider_error") if candidate == "astra" else decision,
        )
    lab.record_outcome("test", "one", outcome)
    report = lab.report("test")["candidates"]
    assert report["astra"]["errors"] == 1
    assert report["astra"]["completed_episodes"] == 0
    assert report["claude"]["completed_episodes"] == 1


def test_manifest_frozen_and_budget_enforced(setup):
    lab, config, packet, _, _ = setup
    with pytest.raises(sqlite3.IntegrityError):
        lab.create("test", dict(config, fee_bps="0"))
    lab.add_case("test", "two", packet)
    with pytest.raises(ValueError, match="case_budget"):
        lab.add_case("test", "three", packet)


def test_candidate_version_cannot_change_mid_experiment(setup):
    lab, _, packet, decision, _ = setup
    lab.record_decision("test", "one", "astra", decision)
    lab.add_case("test", "two", packet)
    with pytest.raises(ValueError, match="candidate_version_changed"):
        lab.record_decision("test", "two", "astra", dict(decision, model_version="new"))


def test_missing_and_pending_evidence_remains_visible(setup):
    lab, _, _, decision, _ = setup
    lab.record_decision("test", "one", "astra", decision)
    report = lab.report("test")["candidates"]
    assert report["astra"]["pending_buy_outcomes"] == 1
    assert report["claude"]["missing_decisions"] == 1


def test_export_reopens_readonly_and_hashes_same_evidence(setup):
    lab, _, _, decision, outcome = setup
    record_all(lab, decision)
    lab.record_outcome("test", "one", outcome)
    evidence = lab.export_evidence("test")
    assert evidence["sha256"] == hashlib.sha256(canonical(evidence["body"]).encode()).hexdigest()
    path = lab.db.execute("PRAGMA database_list").fetchone()[2]
    reader = ResearchLab(path, readonly=True)
    try:
        assert reader.export_evidence("test") == evidence
        assert reader.report("test") == lab.report("test")
        with pytest.raises(sqlite3.OperationalError):
            reader.db.execute("DELETE FROM decisions")
    finally:
        reader.close()


def test_wrong_database_is_never_modified(tmp_path):
    path = tmp_path / "paper.sqlite"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE paper_accounts (cash TEXT)")
    db.commit()
    db.close()
    original = path.read_bytes()
    with pytest.raises(ValueError, match="not_a_research_database"):
        ResearchLab(path)
    assert path.read_bytes() == original


def test_missing_readonly_database_not_created(tmp_path):
    path = tmp_path / "missing.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        ResearchLab(path, readonly=True)
    assert not path.exists()


@pytest.mark.parametrize("target", ["packet", "config", "decision"])
def test_evidence_tampering_detected(setup, target):
    lab, _, _, decision, _ = setup
    lab.record_decision("test", "one", "astra", decision)
    with lab.db:
        if target == "packet":
            lab.db.execute("UPDATE cases SET packet='{}'")
        elif target == "config":
            lab.db.execute("UPDATE experiments SET config='{}'")
        else:
            lab.db.execute(
                "UPDATE decisions SET body=?", (canonical(dict(decision, input_digest="bad")),)
            )
    with pytest.raises(ValueError, match="integrity_failure"):
        lab.report("test")


def test_html_escapes_untrusted_candidate_and_omits_raw_inputs(setup):
    from quant_ai.research.reporting import diagnostics, render_html

    lab, _, _, _, _ = setup
    report = lab.report("test")
    report["experiment"] = "<script>alert(1)</script>"
    report["candidates"]["<img src=x onerror=alert(1)>"] = report["candidates"].pop("astra")
    page = render_html(report)
    assert "<script>" not in page
    assert "<img" not in page
    assert "&lt;script&gt;" in page
    assert "synthetic-test-only" not in page
    assert "default-src 'none'" in page
    assert diagnostics(report)["winner"] is None
    assert "case_collection_incomplete" in diagnostics(report)["comparison_blockers"]


def test_cost_stress_adverse_and_no_fake_exit(setup):
    from decimal import Decimal

    from quant_ai.research.reporting import cost_stress

    _, config, _, decision, outcome = setup
    results = cost_stress(config, decision, outcome)
    assert [r["multiplier"] for r in results] == ["1", "2", "3"]
    assert Decimal(results[0]["net_pnl_inr"]) > Decimal(results[-1]["net_pnl_inr"])
    outcome["exit_available_quantity"] = 0
    assert all(r["net_pnl_inr"] is None for r in cost_stress(config, decision, outcome))
    with pytest.raises(ValueError, match="stress_must_not_reduce_costs"):
        cost_stress(config, decision, outcome, [0.5])
