"""Adversarial certification of recorded-entry coverage, never capacity release."""
import json
from dataclasses import replace
from decimal import Decimal

import pytest
from test_institutional_paper_coordinator import NOW, Harness, make_request, proposal
from test_shared_risk_committed_coverage import _completed, _corrupt, restored_older_journal
from test_shared_risk_reservations import enable

from quant_ai.execution.shared_risk_binding import verify_binding_pair


@pytest.mark.parametrize("field,value", [
    ("schema", "not-a-fill"), ("schema", None),
    ("event_type", "research_observation"), ("subject", "TCS"),
])
def test_recorded_receipt_requires_exact_fill_schema_and_subject(tmp_path, field, value):
    h, _ = _completed(tmp_path)
    try:
        db = h.broker._connection
        payload = json.loads(db.execute("SELECT payload FROM paper_decision_evidence").fetchone()[0])
        payload[field] = value
        _corrupt(db, "paper_decision_evidence", "UPDATE paper_decision_evidence SET payload=?",
                 (json.dumps(payload),))
        before = tuple(db.iterdump()), tuple(h.programs.db.iterdump())
        with pytest.raises(ValueError, match="shared_risk_broker_committed_entry"):
            verify_binding_pair(db, h.programs.db, "tenant")
        assert before == (tuple(db.iterdump()), tuple(h.programs.db.iterdump()))
    finally:
        h.close()


@pytest.mark.parametrize("value", [100, 100.0, True, None, "NaN", "Infinity"])
def test_receipt_fill_price_must_be_finite_decimal_text(tmp_path, value):
    h, _ = _completed(tmp_path)
    try:
        db = h.broker._connection
        payload = json.loads(db.execute("SELECT payload FROM paper_decision_evidence").fetchone()[0])
        payload["fill"]["price"] = value
        _corrupt(db, "paper_decision_evidence", "UPDATE paper_decision_evidence SET payload=?",
                 (json.dumps(payload),))
        with pytest.raises(ValueError, match="shared_risk_broker_committed_entry"):
            verify_binding_pair(db, h.programs.db, "tenant")
    finally:
        h.close()


@pytest.mark.parametrize("table,field,value", [
    ("execution_programs", "parent_quantity", 11),
    ("execution_programs", "symbol", "TCS"),
    ("execution_programs", "state", "PLANNED"),
    ("execution_programs", "state", "NOT_A_STATE"),
    ("execution_program_slices", "failure_reason", "contradictory recorded failure"),
    ("execution_programs", "created_at", "2999-01-01T00:00:00+00:00"),
])
def test_claim_history_cannot_contradict_the_committed_parent(tmp_path, table, field, value):
    h, _ = _completed(tmp_path)
    try:
        _corrupt(h.programs.db, table, f"UPDATE {table} SET {field}=?", (value,))
        before = tuple(h.programs.db.iterdump())
        with pytest.raises(ValueError, match="shared_risk_broker_committed_entry"):
            verify_binding_pair(h.broker._connection, h.programs.db, "tenant")
        assert tuple(h.programs.db.iterdump()) == before
    finally:
        h.close()


@pytest.mark.parametrize("field,value", [
    ("margin_change", "1"), ("margin_provenance", "{\"synthetic\":true}"),
])
def test_cash_entry_coverage_rejects_derivative_collateral_metadata(tmp_path, field, value):
    h, _ = _completed(tmp_path)
    try:
        _corrupt(h.broker._connection, "paper_ledger", f"UPDATE paper_ledger SET {field}=?", (value,))
        with pytest.raises(ValueError, match="shared_risk_broker_committed_entry"):
            verify_binding_pair(h.broker._connection, h.programs.db, "tenant")
    finally:
        h.close()


def test_valid_partial_parent_survives_later_slice_risk_rejection(tmp_path):
    from datetime import timedelta

    from quant_ai.execution.planner import ExecutionAlgorithm, VolumeBucket
    h = Harness(tmp_path)
    try:
        enable(h)
        request = make_request(h.broker, algorithm=ExecutionAlgorithm.TWAP,
            buckets=(VolumeBucket(NOW, 1000), VolumeBucket(NOW + timedelta(minutes=5), 1000)))
        prepared = h.coordinator.prepare(request)
        first = h.coordinator.execute_due(prepared.program.program_id, now=NOW)
        assert first.executed_sequences == (1,)
        h.daily_total_pnl = Decimal(-50000)
        last = h.coordinator.execute_due(prepared.program.program_id, now=NOW + timedelta(minutes=5))
        assert last.stage.value == "FAILED"
        assert verify_binding_pair(h.broker._connection, h.programs.db, "tenant") is not None
        assert len(h.broker.ledger_entries("tenant")) == 1
    finally:
        h.close()


def test_valid_coverage_remains_after_protective_exit_and_accounting(tmp_path):
    from quant_ai.execution.protective_exits import ProtectiveExitEngine
    h, _ = _completed(tmp_path)
    try:
        outcomes = ProtectiveExitEngine(h.broker, lambda _: Decimal(90), tenant_id="tenant").evaluate()
        assert outcomes[0].filled
        assert h.coordinator.reconcile_protective_accounting(currency="INR").status == "matched"
        assert verify_binding_pair(h.broker._connection, h.programs.db, "tenant") is not None
        assert h.broker.get_positions("tenant") == ()
    finally:
        h.close()


def test_reopened_account_still_cannot_forget_an_earlier_committed_entry(tmp_path):
    h, _ = restored_older_journal(tmp_path)
    h.close()
    restarted = Harness(tmp_path)
    try:
        enable(restarted)
        r = make_request(restarted.broker, p=proposal(decision_id="new-process"))
        r = replace(r, projected_factor_positions=restarted.factor_provider(r.proposal, r.portfolio))
        result = restarted.coordinator.prepare(r)
        assert not result.approved and "committed_entry" in result.reason
        assert len(restarted.broker.ledger_entries("tenant")) == 1
    finally:
        restarted.close()
