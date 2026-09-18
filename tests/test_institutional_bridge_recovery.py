"""Synthetic recovery through the public bridge; never start a daemon or live broker."""
from dataclasses import replace
from decimal import Decimal

import pytest
from test_institutional_swarm_bridge import BridgeHarness

from quant_ai.orders.state import OrderState


def interrupted_fill(h, monkeypatch):
    submit = h.broker.submit_with_evidence
    def interruption(*args, **kwargs):
        submit(*args, **kwargs)
        raise ValueError("synthetic interruption after committed paper fill")
    monkeypatch.setattr(h.broker, "submit_with_evidence", interruption)
    result = h.execute()
    assert result.order_state is OrderState.SUBMISSION_UNCERTAIN
    assert len(h.broker.ledger_entries("tenant")) == 1
    monkeypatch.setattr(h.broker, "submit_with_evidence", submit)
    return h.programs.db.execute("SELECT program_id FROM execution_programs").fetchone()[0]


def test_bridge_recovery_reconstructs_committed_fill_without_dispatch_or_live_inputs(tmp_path, monkeypatch):
    h = BridgeHarness(tmp_path)
    try:
        pid = interrupted_fill(h, monkeypatch)
        def forbidden(*args, **kwargs):
            pytest.fail("Historical recovery must not submit or request current trading inputs")
        h.inputs = replace(h.inputs, request_provider=forbidden, factor_position_provider=forbidden,
                           strategy_exposure_provider=forbidden, slice_volume_provider=forbidden)
        fresh = h.new_runtime()
        fresh.pre_submit_check = None
        fresh.snapshot_provider = forbidden
        monkeypatch.setattr(h.broker, "submit_with_evidence", forbidden)
        report = fresh.reconcile_program(pid, tenant_id="tenant")
        assert report.program_state == "COMPLETE"
        assert report.recovered_sequences == (1,)
        assert report.committed_order_ids == (h.broker.ledger_entries("tenant")[0].order_id,)
        assert report.execution_authorized is False
        assert report.source_revision == "synthetic-inputs-v1"
        assert fresh.reconcile_program(pid, tenant_id="tenant").recovered_sequences == ()
        assert h.journal.native_balance("tenant", "CASH_AVAILABLE", "INR") == Decimal(99000)
        assert h.journal.native_balance("tenant", "SECURITIES_COST", "INR") == Decimal(1000)
        assert len(h.broker.ledger_entries("tenant")) == 1
    finally:
        h.close()


def test_bridge_recovery_never_turns_absent_receipt_into_permission_to_retry(tmp_path, monkeypatch):
    h = BridgeHarness(tmp_path)
    try:
        calls = []
        def preflight(_):
            calls.append(1)
            return None if len(calls) == 1 else "synthetic final entry hold"
        h.runtime.pre_submit_check = preflight
        result = h.execute()
        assert result.order_state is OrderState.SUBMISSION_UNCERTAIN
        pid = h.programs.db.execute("SELECT program_id FROM execution_programs").fetchone()[0]
        monkeypatch.setattr(h.broker, "submit_with_evidence", lambda *a, **k: pytest.fail("No retry"))
        report = h.new_runtime().reconcile_program(pid, tenant_id="tenant")
        assert report.recovery_stage == "RECOVERY_REQUIRED"
        assert report.committed_order_ids == () and report.recovered_sequences == ()
        assert report.execution_authorized is False
        assert h.programs.recovery_required("tenant")
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        h.close()



def test_complete_programme_is_not_proof_of_present_accounting(tmp_path):
    h = BridgeHarness(tmp_path)
    try:
        result = h.execute()
        assert result.fill is not None
        pid = h.programs.db.execute("SELECT program_id FROM execution_programs").fetchone()[0]
        for table in ("trading_postings", "trading_transactions"):
            h.journal.db.execute(f"DROP TRIGGER {table}_delete_blocked")
            h.journal.db.execute(f"DELETE FROM {table} WHERE transaction_id=?", ("trade:" + result.fill.order_id,))
        h.journal.db.commit()
        before = tuple(h.journal.db.iterdump())
        with pytest.raises(ValueError, match="institutional_bridge_recovery_accounting_mismatch"):
            h.new_runtime().reconcile_program(pid, tenant_id="tenant")
        assert tuple(h.journal.db.iterdump()) == before
        assert len(h.broker.ledger_entries("tenant")) == 1
    finally:
        h.close()



@pytest.mark.parametrize("field", ["input_matrix", "declared_rationales", "risk_verdict", "provenance"])
def test_changed_committed_decision_trace_refuses_before_any_recovery_write(tmp_path, monkeypatch, field):
    import json
    h = BridgeHarness(tmp_path)
    try:
        pid = interrupted_fill(h, monkeypatch)
        row = h.broker._connection.execute("SELECT order_id,payload FROM paper_decision_evidence").fetchone()
        payload = json.loads(row["payload"])
        payload[field] = {"synthetic_tamper": "not the approved trace"}
        h.broker._connection.execute("UPDATE paper_decision_evidence SET payload=? WHERE order_id=?",
                                    (json.dumps(payload), row["order_id"]))
        h.broker._connection.commit()
        before = (tuple(h.oms.db.iterdump()), tuple(h.journal.db.iterdump()), tuple(h.programs.db.iterdump()))
        with pytest.raises(ValueError, match="accounting_recovery_decision_trace_mismatch"):
            h.new_runtime().reconcile_program(pid, tenant_id="tenant")
        assert before == (tuple(h.oms.db.iterdump()), tuple(h.journal.db.iterdump()), tuple(h.programs.db.iterdump()))
        assert len(h.broker.ledger_entries("tenant")) == 1
    finally:
        h.close()


@pytest.mark.parametrize("fault", ["unknown_tenant", "unknown_programme"])
def test_unknown_recovery_target_does_not_change_valid_runtime_binding(tmp_path, monkeypatch, fault):
    h = BridgeHarness(tmp_path)
    try:
        pid = interrupted_fill(h, monkeypatch)
        runtime = h.new_runtime()
        before = tuple(h.journal.db.iterdump())
        with pytest.raises(KeyError):
            runtime.reconcile_program(pid if fault == "unknown_tenant" else "missing", 
                                      tenant_id="other" if fault == "unknown_tenant" else "tenant")
        assert tuple(h.journal.db.iterdump()) == before
        assert runtime._coordinator._requests == {}
        assert runtime.reconcile_program(pid, tenant_id="tenant").program_state == "COMPLETE"
    finally:
        h.close()


def test_unrelated_coordinator_programme_cannot_be_adopted_as_a_bridge_decision(tmp_path):
    from test_institutional_paper_coordinator import make_request, proposal

    from quant_ai.agents.swarm import InstrumentBoundTradeProposal
    h = BridgeHarness(tmp_path)
    try:
        bound = InstrumentBoundTradeProposal(**vars(proposal()), instrument=h.instrument)
        prepared = h.coordinator.prepare(make_request(h.broker, p=bound))
        assert prepared.approved
        before = tuple(h.programs.db.iterdump())
        with pytest.raises(ValueError, match="institutional_bridge_recovery_context_required"):
            h.runtime.reconcile_program(prepared.program.program_id, tenant_id="tenant")
        assert tuple(h.programs.db.iterdump()) == before
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        h.close()


def test_changed_current_risk_policy_does_not_reauthorize_or_block_historical_accounting(tmp_path, monkeypatch):
    h = BridgeHarness(tmp_path)
    try:
        pid = interrupted_fill(h, monkeypatch)
        h.inputs = replace(h.inputs, source_revision="synthetic-next-provider",
                           edge_policy=replace(h.inputs.edge_policy, max_risk_fraction=Decimal(".001")))
        runtime = h.new_runtime()
        runtime.kill_switch.engage("retain this entry halt")
        report = runtime.reconcile_program(pid, tenant_id="tenant")
        assert report.source_revision == "synthetic-inputs-v1"
        assert report.program_state == "COMPLETE" and report.execution_authorized is False
        assert runtime.kill_switch.engaged
        assert runtime.kill_switch.reason == "retain this entry halt"
    finally:
        h.close()


def test_posting_failure_remains_recoverable_and_does_not_claim_completion(tmp_path, monkeypatch):
    h = BridgeHarness(tmp_path)
    try:
        pid = interrupted_fill(h, monkeypatch)
        original = h.accounting.buy_security
        def unavailable(*args, **kwargs):
            raise ValueError("synthetic accounting unavailable")
        monkeypatch.setattr(h.accounting, "buy_security", unavailable)
        runtime = h.new_runtime()
        with pytest.raises(ValueError, match="synthetic accounting unavailable"):
            runtime.reconcile_program(pid, tenant_id="tenant")
        assert h.programs.get(pid).slices[0].state.value == "FILLED_UNACCOUNTED"
        assert h.journal.native_balance("tenant", "CASH_AVAILABLE", "INR") == Decimal(100000)
        monkeypatch.setattr(h.accounting, "buy_security", original)
        assert runtime.reconcile_program(pid, tenant_id="tenant").program_state == "COMPLETE"
        assert len(h.broker.ledger_entries("tenant")) == 1
    finally:
        h.close()


def test_already_protected_fill_recovers_without_reopening_or_releasing_risk(tmp_path, monkeypatch):
    from quant_ai.execution.protective_exits import ProtectiveExitEngine
    from quant_ai.execution.shared_risk import verify_shared_risk
    h = BridgeHarness(tmp_path)
    try:
        pid = interrupted_fill(h, monkeypatch)
        reservation = verify_shared_risk(h.programs.db, "tenant")
        exit_ = ProtectiveExitEngine(h.broker, lambda _: Decimal(90), tenant_id="tenant").evaluate()
        assert len(exit_) == 1 and exit_[0].filled
        historical_order = h.broker.ledger_entries("tenant")[0].order_id
        report = h.new_runtime().reconcile_program(pid, tenant_id="tenant")
        assert report.committed_order_ids == (historical_order,)
        assert report.program_state == "COMPLETE"
        assert h.broker.get_positions("tenant") == ()
        assert len(h.broker.ledger_entries("tenant")) == 2
        assert h.journal.native_balance("tenant", "SECURITIES_COST", "INR") == 0
        assert h.journal.native_balance("tenant", "CASH_AVAILABLE", "INR") == Decimal(99900)
        assert verify_shared_risk(h.programs.db, "tenant") == reservation
    finally:
        h.close()


def test_recovery_reopens_actual_stores_and_keeps_trace_projection_unchanged(tmp_path, monkeypatch):
    h = BridgeHarness(tmp_path)
    pid = interrupted_fill(h, monkeypatch)
    before_proofs = {p.name: p.read_bytes() for p in (tmp_path / "proofs").iterdir()}
    h.close()
    fresh = BridgeHarness(tmp_path)
    try:
        report = fresh.runtime.reconcile_program(pid, tenant_id="tenant")
        assert report.program_state == "COMPLETE"
        assert {p.name: p.read_bytes() for p in (tmp_path / "proofs").iterdir()} == before_proofs
        assert len(fresh.broker.ledger_entries("tenant")) == 1
        assert fresh.runtime.xai_logger.traces() == ()
    finally:
        fresh.close()


def test_wrong_accounting_selection_refuses_before_recovering_committed_receipt(tmp_path, monkeypatch):
    from quant_ai.accounting.journal import TradingJournal
    from quant_ai.accounting.trading import TradingAccounting
    h = BridgeHarness(tmp_path)
    other = TradingJournal(tmp_path / "other.sqlite", base_currency="INR")
    try:
        pid = interrupted_fill(h, monkeypatch)
        h.inputs = replace(h.inputs, accounting=TradingAccounting(other, "tenant"))
        runtime = h.new_runtime()
        before = tuple(other.db.iterdump())
        with pytest.raises(ValueError, match="institutional_accounting_binding_mismatch"):
            runtime.reconcile_program(pid, tenant_id="tenant")
        assert tuple(other.db.iterdump()) == before
        assert h.journal.native_balance("tenant", "CASH_AVAILABLE", "INR") == Decimal(100000)
    finally:
        other.close()
        h.close()


def test_concurrent_explicit_recovery_calls_share_durable_fill_idempotency(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    h = BridgeHarness(tmp_path)
    try:
        pid = interrupted_fill(h, monkeypatch)
        runtime = h.new_runtime()
        with ThreadPoolExecutor(max_workers=2) as pool:
            reports = list(pool.map(lambda _: runtime.reconcile_program(pid, tenant_id="tenant"), range(2)))
        assert all(report.program_state == "COMPLETE" for report in reports)
        assert sorted(report.recovered_sequences for report in reports) == [(), (1,)]
        assert len(h.broker.ledger_entries("tenant")) == 1
        assert h.journal.db.execute("SELECT count(*) FROM trading_transactions WHERE kind='TRADE'").fetchone()[0] == 1
    finally:
        h.close()



def test_false_complete_flag_with_no_executed_slice_is_not_recovery_success(tmp_path):
    h = BridgeHarness(tmp_path)
    try:
        calls = []
        def preflight(_):
            calls.append(1)
            return None if len(calls) == 1 else "synthetic hold"
        h.runtime.pre_submit_check = preflight
        assert h.execute().order_state is OrderState.SUBMISSION_UNCERTAIN
        pid = h.programs.db.execute("SELECT program_id FROM execution_programs").fetchone()[0]
        h.programs.db.execute("UPDATE execution_programs SET state='COMPLETE' WHERE program_id=?", (pid,))
        h.programs.db.commit()
        before = tuple(h.programs.db.iterdump())
        with pytest.raises(ValueError, match="institutional_bridge_recovery_state_mismatch"):
            h.new_runtime().reconcile_program(pid, tenant_id="tenant")
        assert tuple(h.programs.db.iterdump()) == before
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        h.close()


def test_recovered_cash_fees_are_verified_and_missing_completed_fee_is_not_repaired(tmp_path, monkeypatch):
    from quant_ai.execution.friction import BrokerageSchedule
    h = BridgeHarness(tmp_path)
    try:
        h.broker.friction_model.brokerage_schedule = BrokerageSchedule(
            "synthetic-fixed-fee", Decimal(0), Decimal(1), Decimal(1), Decimal(0), Decimal(0))
        pid = interrupted_fill(h, monkeypatch)
        report = h.new_runtime().reconcile_program(pid, tenant_id="tenant")
        assert report.program_state == "COMPLETE"
        costs = [row for row in h.broker.cost_entries("tenant") if row.cash_debit and row.amount > 0]
        assert costs
        assert h.journal.native_balance("tenant", "CASH_AVAILABLE", "INR") == h.broker.get_margin("tenant").cash_balance
        missing = f"cost:{costs[0].order_id}:{costs[0].code}"
        for table in ("trading_postings", "trading_transactions"):
            h.journal.db.execute(f"DROP TRIGGER {table}_delete_blocked")
            h.journal.db.execute(f"DELETE FROM {table} WHERE transaction_id=?", (missing,))
        h.journal.db.commit()
        before = tuple(h.journal.db.iterdump())
        with pytest.raises(ValueError, match="institutional_bridge_recovery_accounting_mismatch"):
            h.new_runtime().reconcile_program(pid, tenant_id="tenant")
        assert tuple(h.journal.db.iterdump()) == before
    finally:
        h.close()


@pytest.mark.parametrize("boundary", ["after_oms", "inside_accounting", "before_program_complete"])
def test_process_death_during_public_recovery_does_not_duplicate_a_trade(tmp_path, monkeypatch, boundary):
    import os
    import subprocess
    import sys
    from pathlib import Path
    h = BridgeHarness(tmp_path)
    pid = interrupted_fill(h, monkeypatch)
    h.close()
    code = r"""
import os, sys
from pathlib import Path
from test_institutional_swarm_bridge import BridgeHarness
root, pid, boundary = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
h = BridgeHarness(root)
target, name = {'after_oms': (h.oms, 'fill'),
                'inside_accounting': (h.accounting, 'buy_security'),
                'before_program_complete': (h.programs, 'mark_executed')}[boundary]
original = getattr(target, name)
def interrupted(*args, **kwargs):
    if boundary != 'before_program_complete':
        original(*args, **kwargs)
    os._exit(73)
setattr(target, name, interrupted)
def forbidden(*args, **kwargs):
    raise AssertionError('Recovery cannot call the broker submit method')
h.broker.submit_with_evidence = forbidden
h.runtime.reconcile_program(pid, tenant_id='tenant')
raise AssertionError('Recovery crash boundary was not reached')
"""
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(root / "src"), str(root / "tests"))),
           "TRADING_LIVE_MONEY_ACTIVE": "false"}
    child = subprocess.run([sys.executable, "-c", code, str(tmp_path), pid, boundary],
                           capture_output=True, text=True, cwd=root, env=env, timeout=30, check=False)
    assert child.returncode == 73, child.stdout + child.stderr
    fresh = BridgeHarness(tmp_path)
    try:
        report = fresh.runtime.reconcile_program(pid, tenant_id="tenant")
        assert report.program_state == "COMPLETE" and report.execution_authorized is False
        assert len(fresh.broker.ledger_entries("tenant")) == 1
        assert fresh.journal.native_balance("tenant", "CASH_AVAILABLE", "INR") == Decimal(99000)
        assert fresh.journal.native_balance("tenant", "SECURITIES_COST", "INR") == Decimal(1000)
        assert fresh.runtime.reconcile_program(pid, tenant_id="tenant").recovered_sequences == ()
    finally:
        fresh.close()
