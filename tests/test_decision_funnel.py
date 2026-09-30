"""Synthetic session evidence only: no provider, broker or runtime operations."""
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from quant_ai.agents.atlas import AtlasInvestmentAgent, AtlasPolicy
from quant_ai.agents.contracts import AgentDomain, AgentEvidence, Stance
from quant_ai.analytics.decision_funnel import report
from quant_ai.analytics.decision_journal import decision_row
from quant_ai.domain.models import AssetClass, Market


def test_session_funnel_counts_each_evaluation_once_and_separates_protection(tmp_path):
    path = tmp_path / "synthetic.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript('''
        CREATE TABLE paper_decision_journal(decision_id TEXT,tenant_id TEXT,symbol TEXT,
            decided_at TEXT,side TEXT,governance TEXT,reason TEXT,order_id TEXT,
            inference_status TEXT,funnel_evidence TEXT);
        CREATE TABLE paper_ledger(order_id TEXT,tenant_id TEXT,symbol TEXT,created_at TEXT);
        CREATE TABLE paper_decision_evidence(order_id TEXT,tenant_id TEXT,payload TEXT);
        CREATE TABLE paper_protection_evidence(order_id TEXT,tenant_id TEXT,payload TEXT);
        CREATE TABLE paper_protective_fill_outbox(order_id TEXT,tenant_id TEXT,receipt_sha256 TEXT);
        ''')
        rows = [
            ("a", "paper", "AAA", "2026-01-04T18:30:00+00:00", None, "abstained", "quorum", None, "not_requested", None),
            ("b", "paper", "AAA", "2026-01-05T09:00:00+05:30", "BUY", "rejected", "StaleMarketData", None, "budget_exhausted", None),
            ("c", "paper", "AAA", "2026-01-05T04:00:00+00:00", "BUY", "filled", None, "order-c", "completed", json.dumps({"order_state": "FILLED"})),
            ("excluded", "paper", "AAA", "2026-01-05T18:30:00+00:00", "BUY", "filled", None, "outside", None, None),
            ("foreign", "other", "AAA", "2026-01-05T04:00:00+00:00", "BUY", "filled", None, "foreign", None, None),
        ]
        db.executemany("INSERT INTO paper_decision_journal VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
        db.executemany("INSERT INTO paper_ledger VALUES(?,?,?,?)", [
            ("order-c", "paper", "AAA", "2026-01-05T04:00:00+00:00"),
            ("exit", "paper", "AAA", "2026-01-05T05:00:00+00:00"),
            ("foreign", "other", "AAA", "2026-01-05T04:00:00+00:00")])
        db.execute("INSERT INTO paper_decision_evidence VALUES(?,?,?)", ("order-c", "paper", '{"decision_id":"c"}'))
        db.execute("INSERT INTO paper_protection_evidence VALUES(?,?,?)", ("exit", "paper", '{"decision_id":"protect"}'))
        db.execute("INSERT INTO paper_protective_fill_outbox VALUES(?,?,?)", ("exit", "paper", "synthetic"))
    before = path.read_bytes()
    result = report(path, "paper", "2026-01-05")
    assert path.read_bytes() == before
    assert result["evaluations"] == 3
    assert result["directional_proposals"] == 2
    assert result["outcomes"] == {"abstained": 1, "rejected": 1, "filled": 1}
    assert sum(result["outcomes"].values()) == result["evaluations"]
    assert result["inference_statuses"]["budget_exhausted"] == 1  # A dimension, not fourth outcome.
    assert result["ledger_fills"] == 2
    assert result["fill_kinds"] == {"swarm": 1, "protection": 1}
    exit_link = next(link for link in result["fill_links"] if link["order_id"] == "exit")
    assert exit_link["journal_decision_ids"] == []
    assert exit_link["protective_outbox_present"] is True
    assert result["evaluation_records"][0]["evidence"] is None


def test_missing_tables_are_unknown_not_zero(tmp_path):
    path = tmp_path / "empty.sqlite"
    sqlite3.connect(path).close()
    result = report(path, "paper", "2026-01-05")
    assert result["evaluations"] is None
    assert result["ledger_fills"] is None


def test_effective_quorum_snapshot_preserves_gate_and_stale_hold():
    now = datetime(2026, 1, 5, 4, tzinfo=timezone.utc)
    def evidence(agent, domain, stance, age, confidence=".8"):
        return AgentEvidence(agent, domain, "AAA", stance, Decimal(confidence),
                             Decimal(".02"), Decimal(".01"), ("synthetic",), now, age)
    inputs = (evidence("a", AgentDomain.COUNTRY, Stance.BUY, 3601),
              evidence("b", AgentDomain.TECHNICAL, Stance.BUY, 0),
              evidence("c", AgentDomain.NEWS, Stance.NEUTRAL, 0, "0"),
              evidence("gate", AgentDomain.RISK, Stance.NEUTRAL, 0))
    atlas = AtlasInvestmentAgent(policy=AtlasPolicy())
    decision = atlas.decide("AAA", inputs, now)
    assert decision.rationale[0] == "stale_specialist_evidence"
    snapshot = decision.provenance["evidence_admission"]
    assert snapshot["minimum_voters"] == 3
    assert snapshot["voter_count"] == 3
    assert snapshot["coverage_candidates"] == 2  # Candidates do not mean admitted votes.
    assert snapshot["agents"][0]["age_seconds"] == 3601
    assert snapshot["agents"][0]["budget_seconds"] == 3600
    assert snapshot["agents"][-1]["role"] == "gate"


@pytest.mark.parametrize("trace_fields,expected_id", [({}, None), ({"decision_id": None}, None),
                                                     ({"decision_id": "distinct-proof"}, "distinct-proof")])
def test_journal_projection_links_proof_and_execution_state_without_provider_body(trace_fields, expected_id):
    now = datetime(2026, 1, 5, 4, tzinfo=timezone.utc)
    proposal = SimpleNamespace(decision_id="synthetic", symbol="AAA", market=Market.INDIA,
        asset_class=AssetClass.EQUITY, side=None, quantity=0, confidence=Decimal(0),
        expected_return=Decimal(0), expected_risk=Decimal(0), reference_price=Decimal(1),
        stop_price=None, take_profit_price=None, provenance={})
    trace = SimpleNamespace(**trace_fields, input_matrix=(), provenance={
        "inputs_sha256": "a" * 64, "evidence_admission": {"minimum_voters": 3},
        "provider_body": "not persisted"})
    result = SimpleNamespace(proposal=proposal, xai_trace=trace, fill=None,
        risk_decision=SimpleNamespace(approved=False, reason="atlas_non_actionable_proposal"),
        order_state="REJECTED")
    row = decision_row(result, tenant_id="paper", now=now)
    snapshot = json.loads(row["funnel_evidence"])
    assert snapshot["proof_decision_id"] == expected_id
    assert row["decision_id"] == "synthetic"
    assert snapshot["order_state"] == "REJECTED"
    assert snapshot["admission"]["minimum_voters"] == 3
    assert "not persisted" not in row["funnel_evidence"]


@pytest.mark.parametrize("evidence,expected", [
    ({"deterministic_gate_reasons": []}, {}),
    ({}, {"unknown": 1}),
    ({"deterministic_gate_reasons": None}, {"unknown": 1}),
    ({"deterministic_gate_reasons": "stale"}, {"unknown": 1}),
    ({"deterministic_gate_reasons": [None]}, {"unknown": 1}),
    ({"deterministic_gate_reasons": [""]}, {"unknown": 1}),
    ("malformed JSON", {"unknown": 1}),
    ({"deterministic_gate_reasons": ["stale", "quorum", "stale"]}, {"stale": 2, "quorum": 1}),
])
def test_report_preserves_known_empty_gate_reasons(tmp_path, evidence, expected):
    path = tmp_path / "gate-reasons.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE paper_decision_journal(tenant_id TEXT, decided_at TEXT, funnel_evidence TEXT, decision_id TEXT, symbol TEXT)")
        payload = evidence if isinstance(evidence, str) else json.dumps(evidence)
        db.execute("INSERT INTO paper_decision_journal VALUES(?,?,?,?,?)",
                   ("paper", "2026-01-05T04:00:00+00:00", payload, "synthetic", "AAA"))
    assert report(path, "paper", "2026-01-05")["deterministic_gate_reasons"] == expected
