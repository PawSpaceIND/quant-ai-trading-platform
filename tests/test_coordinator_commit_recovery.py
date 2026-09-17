"""Synthetic-only crash regressions; never use an operator ledger or a network broker."""
import os
import subprocess
import sys
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest
from test_institutional_paper_coordinator import NOW, Harness, make_request, proposal

from quant_ai.domain.models import Side


class SimulatedCrash(BaseException):
    pass


@pytest.mark.parametrize("boundary", ["broker_return", "oms_fill", "program_record"])
@pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
def test_committed_fill_is_recovered_even_before_program_marker(tmp_path, monkeypatch, boundary, side):
    h = Harness(tmp_path)
    if side is Side.SELL:
        entry = h.coordinator.prepare(make_request(h.broker))
        assert h.coordinator.execute_due(entry.program.program_id, now=NOW).stage.value == "COMPLETE"
    p = replace(proposal(side=side, decision_id="crash-trade"), reference_price=proposal().reference_price)
    request = make_request(h.broker, p=p)
    prep = h.coordinator.prepare(request)
    assert prep.approved
    program_id, parent = prep.program.program_id, prep.approved_order
    target, method = {"broker_return": (h.broker, "submit_with_evidence"),
                      "oms_fill": (h.oms, "fill"),
                      "program_record": (h.programs, "mark_filled_unaccounted")}[boundary]
    original = getattr(target, method)
    def crash(*args, **kwargs):
        if boundary == "broker_return":
            original(*args, **kwargs)
        raise SimulatedCrash(boundary)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(target, method, crash)
            with pytest.raises(SimulatedCrash):
                h.coordinator.execute_due(program_id, now=NOW)
        assert len(h.broker.ledger_entries("tenant")) == (2 if side is Side.SELL else 1)
    finally:
        h.close()
    h = Harness(tmp_path)
    try:
        h.coordinator.bind_runtime_context(program_id, request=request, parent_order=parent)
        before = tuple(h.broker._connection.iterdump())
        result = h.coordinator.reconcile_accounting(program_id)
        assert result.stage.value == "COMPLETE"
        assert result.program.executed_quantity == parent.quantity
        assert tuple(h.broker._connection.iterdump()) == before
        assert h.journal.native_balance("tenant", "CASH_AVAILABLE", "INR") == h.broker.get_margin("tenant").cash_balance
        assert h.oms.verify(result.program.slices[0].client_order_id)["verified"]
        assert h.journal.verify("tenant")["verified"]
        accounting = tuple(h.journal.db.iterdump())
        assert h.coordinator.reconcile_accounting(program_id).stage.value == "COMPLETE"
        assert tuple(h.journal.db.iterdump()) == accounting
    finally:
        h.close()


@pytest.mark.parametrize("boundary", ["create", "approve_risk", "submitted"])
def test_pre_submission_crash_is_unresolved_never_retried(tmp_path, monkeypatch, boundary):
    h = Harness(tmp_path)
    try:
        request = make_request(h.broker)
        prep = h.coordinator.prepare(request)
        original = getattr(h.oms, boundary)
        def crash(*args, **kwargs):
            original(*args, **kwargs)
            raise SimulatedCrash(boundary)
        with monkeypatch.context() as patch:
            patch.setattr(h.oms, boundary, crash)
            with pytest.raises(SimulatedCrash):
                h.coordinator.execute_due(prep.program.program_id, now=NOW)
        def forbidden(*args, **kwargs):
            raise AssertionError("Recovery must never submit an order")
        monkeypatch.setattr(h.broker, "submit_with_evidence", forbidden)
        recovered = h.coordinator.reconcile_accounting(prep.program.program_id)
        assert recovered.stage.value == "RECOVERY_REQUIRED"
        assert h.coordinator.execute_due(prep.program.program_id, now=NOW).stage.value == "RECOVERY_REQUIRED"
        assert h.broker.ledger_entries("tenant") == ()
        # Another program on this tenant cannot leap over the unresolved submission.
        other = h.coordinator.prepare(make_request(h.broker, p=proposal(decision_id="another")))
        assert h.coordinator.execute_due(other.program.program_id, now=NOW).stage.value == "RECOVERY_REQUIRED"
    finally:
        h.close()


def test_exception_after_broker_commit_is_not_recorded_as_rejected(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    try:
        req = make_request(h.broker)
        prep = h.coordinator.prepare(req)
        original = h.broker.submit_with_evidence
        def commit_then_error(*args, **kwargs):
            original(*args, **kwargs)
            raise ValueError("synthetic_error_after_commit")
        with monkeypatch.context() as patch:
            patch.setattr(h.broker, "submit_with_evidence", commit_then_error)
            result = h.coordinator.execute_due(prep.program.program_id, now=NOW)
            assert result.stage.value == "RECOVERY_REQUIRED"
        assert h.coordinator.reconcile_accounting(prep.program.program_id).stage.value == "COMPLETE"
        assert len(h.broker.ledger_entries("tenant")) == 1
        assert h.oms.get(result.program.slices[0].client_order_id).state.value == "FILLED"
    finally:
        h.close()


def test_broker_receipt_captures_real_pre_fill_basis_not_callers_copy(tmp_path):
    from quant_ai.domain.models import AssetClass, Market, OrderIntent
    from quant_ai.execution.paper_ledger import PaperBrokerService
    with_broker = PaperBrokerService(tmp_path / "receipt.sqlite", slippage_bps=D(0))
    try:
        order = OrderIntent("INFY", Market.INDIA, Side.BUY, 10, D(100), "test", AssetClass.EQUITY, "tenant")
        forged = {"schema": "pramana.swarm_fill.v1", "event_type": "swarm_fill",
                  "paper_submission_receipt": {"priorAverage": "999999"}}
        with_broker.submit_with_evidence(order, forged, "entry")
        sale = replace(order, side=Side.SELL, reference_price=D(120))
        with_broker.submit_with_evidence(sale, forged, "exit")
        receipt = with_broker.submission_receipt("exit", "tenant")
        assert receipt.prior_quantity == 10 and receipt.prior_average == D(100)
        assert receipt.order == sale
        assert with_broker.get_positions("tenant") == ()
        assert with_broker.get_margin("tenant").cash_balance == D(100200)
    finally:
        with_broker.close()


@pytest.mark.parametrize("defect", ["program", "slice", "intent", "basis", "missing_receipt", "claim"])
def test_corrupt_receipt_never_generates_accounting(tmp_path, monkeypatch, defect):
    import json
    h = Harness(tmp_path)
    try:
        prep = h.coordinator.prepare(make_request(h.broker))
        def crash(*args, **kwargs):
            raise SimulatedCrash()
        with monkeypatch.context() as patch:
            patch.setattr(h.oms, "fill", crash)
            with pytest.raises(SimulatedCrash):
                h.coordinator.execute_due(prep.program.program_id, now=NOW)
        payload = json.loads(h.broker._connection.execute("SELECT payload FROM paper_decision_evidence").fetchone()[0])
        if defect == "program":
            payload["institutional_program"] = "other"
        elif defect == "slice":
            payload["institutional_slice"] = 2
        elif defect == "intent":
            payload["order_intent_sha256"] = "0" * 64
        elif defect == "basis":
            payload["paper_submission_receipt"]["priorQuantity"] = 1
        elif defect == "missing_receipt":
            payload.pop("paper_submission_receipt")
        with h.broker._connection:
            if defect == "claim":
                h.broker._connection.execute("DELETE FROM paper_idempotency")
            else:
                h.broker._connection.execute("UPDATE paper_decision_evidence SET payload=?", (json.dumps(payload),))
        before_oms = tuple(h.oms.db.iterdump())
        before_accounting = tuple(h.journal.db.iterdump())
        with pytest.raises(ValueError):
            h.coordinator.reconcile_accounting(prep.program.program_id)
        assert tuple(h.oms.db.iterdump()) == before_oms
        assert tuple(h.journal.db.iterdump()) == before_accounting
    finally:
        h.close()


def test_pending_accounting_blocks_later_slices_until_recovery(tmp_path):
    from test_institutional_paper_coordinator import FailOnceAccounting

    from quant_ai.execution.planner import ExecutionAlgorithm, VolumeBucket
    h = Harness(tmp_path, accounting_cls=FailOnceAccounting)
    try:
        req = make_request(h.broker, p=proposal(quantity=20), algorithm=ExecutionAlgorithm.TWAP,
                           buckets=(VolumeBucket(NOW, 1000), VolumeBucket(NOW + timedelta(minutes=10), 1000)))
        prep = h.coordinator.prepare(req)
        assert h.coordinator.execute_due(prep.program.program_id, now=NOW).reason.startswith("accounting_reconciliation_required")
        assert h.coordinator.execute_due(prep.program.program_id, now=NOW + timedelta(minutes=10)).stage.value == "RECOVERY_REQUIRED"
        assert len(h.broker.ledger_entries("tenant")) == 1
        assert h.coordinator.reconcile_accounting(prep.program.program_id).stage.value == "READY"
        assert h.coordinator.execute_due(prep.program.program_id, now=NOW + timedelta(minutes=10)).stage.value == "COMPLETE"
        assert len(h.broker.ledger_entries("tenant")) == 2
    finally:
        h.close()


def test_slice_claim_is_exclusive_across_database_connections(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from quant_ai.execution.program import ExecutionProgramJournal
    h = Harness(tmp_path)
    other = ExecutionProgramJournal(tmp_path / "programs.sqlite")
    try:
        req = make_request(h.broker)
        prep = h.coordinator.prepare(req)
        client = h.oms.client_order_id(prep.approved_order, "decision-1:slice:1")
        def claim(journal):
            return journal.claim_slice(prep.program.program_id, 1, client_order_id=client)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, (h.programs, other)))
        assert sorted(results) == [False, True]
        assert h.programs.get(prep.program.program_id).slices[0].state.value == "DISPATCHING"
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        other.close()
        h.close()


@pytest.mark.parametrize("boundary", ["broker", "oms", "program", "accounting", "executed"])
def test_abrupt_process_exit_recovers_the_committed_fill(tmp_path, boundary):
    # os._exit terminates without finally/close/cleanup; all state is in throwaway files.
    code = r"""
import os, sys
from pathlib import Path
from test_institutional_paper_coordinator import Harness, NOW, make_request
root, boundary = Path(sys.argv[1]), sys.argv[2]
h = Harness(root)
prep = h.coordinator.prepare(make_request(h.broker))
(root / 'program-id').write_text(prep.program.program_id)
target, name = {'broker': (h.broker, 'submit_with_evidence'), 'oms': (h.oms, 'fill'),
                'program': (h.programs, 'mark_filled_unaccounted'),
                'accounting': (h.accounting, 'buy_security'),
                'executed': (h.programs, 'mark_executed')}[boundary]
original = getattr(target, name)
def interrupted(*args, **kwargs):
    result = original(*args, **kwargs)
    os._exit(73)
setattr(target, name, interrupted)
h.coordinator.execute_due(prep.program.program_id, now=NOW)
raise AssertionError('crash boundary not reached')
"""
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(root / "src"), str(root / "tests"))),
           "TRADING_LIVE_MONEY_ACTIVE": "false"}
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path), boundary],
                            env=env, capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 73, result.stdout + result.stderr
    from quant_ai.execution.paper_ledger import PaperBrokerService
    from quant_ai.orders.intent import order_from_snapshot
    reference = PaperBrokerService(":memory:", slippage_bps=D(0))
    request = make_request(reference)
    reference.close()
    h = Harness(tmp_path)
    try:
        program_id = (tmp_path / "program-id").read_text()
        parent = order_from_snapshot(h.programs.get(program_id).parent_order_payload)
        h.coordinator.bind_runtime_context(program_id, request=request, parent_order=parent)
        assert h.coordinator.reconcile_accounting(program_id).stage.value == "COMPLETE"
        assert h.journal.native_balance("tenant", "CASH_AVAILABLE", "INR") == D(99000)
        assert h.journal.native_balance("tenant", "SECURITIES_COST", "INR") == D(1000)
        assert len(h.broker.ledger_entries("tenant")) == 1
        assert h.journal.verify("tenant")["verified"]
        assert h.coordinator.reconcile_accounting(program_id).stage.value == "COMPLETE"
    finally:
        h.close()



def test_forged_pre_fill_cost_basis_is_rejected_against_prior_ledger(tmp_path, monkeypatch):
    import json
    h = Harness(tmp_path)
    try:
        initial = h.coordinator.prepare(make_request(h.broker))
        h.coordinator.execute_due(initial.program.program_id, now=NOW)
        sale = h.coordinator.prepare(make_request(h.broker, p=proposal(side=Side.SELL, decision_id="sale")))
        def crash(*args, **kwargs):
            raise SimulatedCrash()
        with monkeypatch.context() as patch:
            patch.setattr(h.oms, "fill", crash)
            with pytest.raises(SimulatedCrash):
                h.coordinator.execute_due(sale.program.program_id, now=NOW)
        row = h.broker._connection.execute("SELECT order_id,payload FROM paper_decision_evidence ORDER BY rowid DESC LIMIT 1").fetchone()
        payload = json.loads(row["payload"])
        payload["paper_submission_receipt"]["priorAverage"] = "50"
        with h.broker._connection:
            h.broker._connection.execute("UPDATE paper_decision_evidence SET payload=? WHERE order_id=?", (json.dumps(payload), row["order_id"]))
        before = tuple(h.journal.db.iterdump())
        with pytest.raises(ValueError, match="paper_receipt_prior_ledger_mismatch"):
            h.coordinator.reconcile_accounting(sale.program.program_id)
        assert tuple(h.journal.db.iterdump()) == before
    finally:
        h.close()



def test_partial_fee_posting_recovers_without_duplicate_cash_debits(tmp_path, monkeypatch):
    from quant_ai.execution.friction import MarketFrictionModel
    h = Harness(tmp_path)
    try:
        h.broker.friction_model = MarketFrictionModel()
        prep = h.coordinator.prepare(make_request(h.broker))
        original = h.accounting.cash_fee
        calls = 0
        def posted_then_error(*args, **kwargs):
            nonlocal calls
            result = original(*args, **kwargs)
            calls += 1
            if calls == 1:
                raise ValueError("synthetic_crash_after_fee_post")
            return result
        with monkeypatch.context() as patch:
            patch.setattr(h.accounting, "cash_fee", posted_then_error)
            result = h.coordinator.execute_due(prep.program.program_id, now=NOW)
            assert result.reason.startswith("accounting_reconciliation_required")
        assert calls == 1
        assert h.coordinator.reconcile_accounting(prep.program.program_id).stage.value == "COMPLETE"
        assert h.journal.native_balance("tenant", "CASH_AVAILABLE", "INR") == h.broker.get_margin("tenant").cash_balance
        assert sum(row.amount for row in h.broker.cost_entries("tenant") if row.cash_debit) > 0
        before = tuple(h.journal.db.iterdump())
        h.coordinator.reconcile_accounting(prep.program.program_id)
        assert tuple(h.journal.db.iterdump()) == before
    finally:
        h.close()


def test_recovery_fence_does_not_disable_independent_protective_exit(tmp_path, monkeypatch):
    from quant_ai.execution.protective_exits import ProtectiveExitEngine
    h = Harness(tmp_path)
    try:
        prep = h.coordinator.prepare(make_request(h.broker))
        h.coordinator.execute_due(prep.program.program_id, now=NOW)
        next_request = make_request(h.broker, p=proposal(decision_id="uncertain"))
        # Include the first committed holding as well as the next purchase.
        next_request = replace(next_request, projected_factor_positions=h.factor_provider(
            next_request.proposal, next_request.portfolio))
        another = h.coordinator.prepare(next_request)
        def crash(*args, **kwargs):
            raise SimulatedCrash()
        with monkeypatch.context() as patch:
            patch.setattr(h.oms, "create", crash)
            with pytest.raises(SimulatedCrash):
                h.coordinator.execute_due(another.program.program_id, now=NOW)
        assert h.programs.recovery_required("tenant")
        exits = ProtectiveExitEngine(h.broker, lambda _: D(90), tenant_id="tenant").evaluate()
        assert len(exits) == 1 and exits[0].filled
        assert h.broker.get_positions("tenant") == ()
        # The uncertainty is retained; this test does not pretend the independent exit
        # has also been mirrored into the coordinator's separate accounting journal.
        assert h.programs.recovery_required("tenant")
    finally:
        h.close()
