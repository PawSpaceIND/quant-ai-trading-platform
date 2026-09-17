"""Synthetic policy-binding tests; no live transport, data source or operator activation."""
from dataclasses import replace
from decimal import Decimal

import pytest
from test_institutional_paper_coordinator import NOW, Harness, make_request

from quant_ai.decision.edge import CalibratedEdgeGate, EdgePolicy

D = Decimal


def test_looser_live_configuration_cannot_expand_prepared_parent_allowance(tmp_path):
    h = Harness(tmp_path)
    try:
        h.coordinator.edge_gate = CalibratedEdgeGate(EdgePolicy(max_risk_fraction=D(".0005")))
        prepared = h.coordinator.prepare(make_request(h.broker))
        assert prepared.approved
        original = h.coordinator.snapshot_provider
        h.coordinator.snapshot_provider = lambda: replace(original(), equity=D(99000))
        h.coordinator.edge_gate = CalibratedEdgeGate(EdgePolicy(max_risk_fraction=D(".001")))
        result = h.coordinator.execute_due(prepared.program.program_id, now=NOW)
        assert result.stage.value == "RECOVERY_REQUIRED"
        assert result.reason == "execution_risk_policy_changed"
        assert h.broker.ledger_entries("tenant") == ()
        assert h.oms.all_orders("tenant") == ()
        assert h.programs.get(prepared.program.program_id).slices[0].state.value == "PENDING"
    finally:
        h.close()


def test_repeated_decision_cannot_silently_change_its_approved_policy(tmp_path):
    h = Harness(tmp_path)
    try:
        request = make_request(h.broker)
        first = h.coordinator.prepare(request)
        assert first.approved
        h.coordinator.edge_gate = CalibratedEdgeGate(EdgePolicy(max_risk_fraction=D(".03")))
        with pytest.raises(ValueError, match="execution_program_decision_payload_mismatch"):
            h.coordinator.prepare(request)
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        h.close()


def test_prepare_retains_policy_payload_in_durable_program(tmp_path):
    import json
    h = Harness(tmp_path)
    try:
        prepared = h.coordinator.prepare(make_request(h.broker))
        program = h.programs.get(prepared.program.program_id)
        raw = program.risk_authority_payload
        assert isinstance(raw, str)
        value = json.loads(raw)
        assert value["policy"]["max_risk_fraction"] == "0.02"
        assert value["mode"] == "ENTRY"
        assert value["tenantId"] == "tenant"
        assert value["programId"] == program.program_id
    finally:
        h.close()


def test_policy_change_inside_input_provider_refuses_before_broker_submission(tmp_path):
    h = Harness(tmp_path)
    try:
        prepared = h.coordinator.prepare(make_request(h.broker))
        original = h.coordinator.factor_position_provider
        def changed(order, portfolio):
            h.coordinator.edge_gate = CalibratedEdgeGate(EdgePolicy(max_risk_fraction=D(".03")))
            return original(order, portfolio)
        h.coordinator.factor_position_provider = changed
        result = h.coordinator.execute_due(prepared.program.program_id, now=NOW)
        assert result.stage.value == "FAILED"
        assert result.reason == "execution_risk_policy_changed"
        assert h.broker.ledger_entries("tenant") == ()
        assert h.oms.all_orders("tenant") == ()
    finally:
        h.close()


def _new_coordinator(h, programs, gate):
    from quant_ai.execution.institutional import InstitutionalPaperCoordinator
    return InstitutionalPaperCoordinator(broker=h.broker, oms=h.oms, programs=programs,
        accounting=h.accounting, warden=h.coordinator.warden,
        snapshot_provider=h.coordinator.snapshot_provider,
        factor_position_provider=h.factor_provider,
        strategy_exposure_provider=lambda _: h.strategy_exposure(),
        slice_volume_provider=lambda *_: 1000, edge_gate=gate)


@pytest.mark.parametrize("field,value", [
    ("min_resolved_samples", 31), ("max_calibration_error", D(".09")),
    ("max_probability_uncertainty", D(".09")), ("min_conservative_edge", D(".002")),
    ("kelly_fraction", D(".5")), ("max_risk_fraction", D(".03")),
])
def test_every_declared_policy_parameter_is_bound(tmp_path, field, value):
    h = Harness(tmp_path)
    try:
        prepared = h.coordinator.prepare(make_request(h.broker))
        h.coordinator.edge_gate = CalibratedEdgeGate(replace(EdgePolicy(), **{field: value}))
        result = h.coordinator.execute_due(prepared.program.program_id, now=NOW)
        assert result.reason == "execution_risk_policy_changed"
        assert result.stage.value == "RECOVERY_REQUIRED"
        assert not h.programs.recovery_required("tenant")  # No dispatch was claimed.
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        h.close()


@pytest.mark.parametrize("invalid", [None, {}, "policy", True])
def test_invalid_policy_configuration_refuses_at_prepare(tmp_path, invalid):
    h = Harness(tmp_path)
    try:
        h.coordinator.edge_gate.policy = invalid
        result = h.coordinator.prepare(make_request(h.broker))
        assert not result.approved and result.reason == "execution_risk_policy_unavailable"
        assert h.programs.db.execute("SELECT COUNT(*) FROM execution_programs").fetchone()[0] == 0
    finally:
        h.close()


def test_new_connection_uses_saved_policy_and_rejects_restart_default_drift(tmp_path):
    from quant_ai.execution.program import ExecutionProgramJournal
    h = Harness(tmp_path)
    try:
        original = CalibratedEdgeGate(EdgePolicy(max_risk_fraction=D(".0005")))
        h.coordinator.edge_gate = original
        request = make_request(h.broker)
        prepared = h.coordinator.prepare(request)
        h.programs.close()
        h.programs = ExecutionProgramJournal(tmp_path / "programs.sqlite")
        restarted = _new_coordinator(h, h.programs, CalibratedEdgeGate())
        restarted.bind_runtime_context(prepared.program.program_id, request=request,
                                       parent_order=prepared.approved_order)
        held = restarted.execute_due(prepared.program.program_id, now=NOW)
        assert held.reason == "execution_risk_policy_changed"
        assert h.broker.ledger_entries("tenant") == ()
        restarted.edge_gate = CalibratedEdgeGate(replace(original.policy))
        result = restarted.execute_due(prepared.program.program_id, now=NOW)
        assert result.stage.value == "COMPLETE"
        assert len(h.broker.ledger_entries("tenant")) == 1
        assert restarted.execute_due(prepared.program.program_id, now=NOW).executed_sequences == ()
    finally:
        h.close()


def test_twap_preserves_first_slice_and_holds_next_after_policy_change(tmp_path):
    from datetime import timedelta

    from test_institutional_paper_coordinator import proposal

    from quant_ai.execution.planner import ExecutionAlgorithm, VolumeBucket
    h = Harness(tmp_path)
    try:
        request = make_request(h.broker, p=proposal(quantity=20), algorithm=ExecutionAlgorithm.TWAP,
            buckets=(VolumeBucket(NOW, 1000), VolumeBucket(NOW + timedelta(minutes=10), 1000)))
        prepared = h.coordinator.prepare(request)
        assert h.coordinator.execute_due(prepared.program.program_id, now=NOW).executed_sequences == (1,)
        h.coordinator.edge_gate = CalibratedEdgeGate(EdgePolicy(kelly_fraction=D(".5")))
        result = h.coordinator.execute_due(prepared.program.program_id, now=NOW + timedelta(minutes=10))
        assert result.reason == "execution_risk_policy_changed"
        assert len(h.broker.ledger_entries("tenant")) == 1
        assert [s.state.value for s in result.program.slices] == ["EXECUTED", "PENDING"]
    finally:
        h.close()


def test_receipt_carries_the_persisted_policy_authority_hash(tmp_path):
    import json
    h = Harness(tmp_path)
    try:
        prepared = h.coordinator.prepare(make_request(h.broker))
        assert h.coordinator.execute_due(prepared.program.program_id, now=NOW).stage.value == "COMPLETE"
        payload = json.loads(h.broker._connection.execute("SELECT payload FROM paper_decision_evidence").fetchone()[0])
        assert payload["institutional_risk_authority_sha256"] == prepared.program.runtime_context_sha256
    finally:
        h.close()


def test_policy_change_does_not_prevent_already_committed_accounting_recovery(tmp_path):
    from test_institutional_paper_coordinator import FailOnceAccounting
    h = Harness(tmp_path, accounting_cls=FailOnceAccounting)
    try:
        prepared = h.coordinator.prepare(make_request(h.broker))
        first = h.coordinator.execute_due(prepared.program.program_id, now=NOW)
        assert first.program.slices[0].state.value == "FILLED_UNACCOUNTED"
        h.coordinator.edge_gate = CalibratedEdgeGate(EdgePolicy(max_risk_fraction=D(".000001")))
        result = h.coordinator.reconcile_accounting(prepared.program.program_id)
        assert result.stage.value == "COMPLETE"
        assert len(h.broker.ledger_entries("tenant")) == 1
        assert h.journal.native_balance("tenant", "SECURITIES_COST", "INR") == D(1000)
    finally:
        h.close()


def test_covered_exit_remains_available_with_unusable_entry_policy(tmp_path):
    from test_institutional_paper_coordinator import proposal

    from quant_ai.domain.models import Side
    h = Harness(tmp_path)
    try:
        buy = h.coordinator.prepare(make_request(h.broker))
        assert h.coordinator.execute_due(buy.program.program_id, now=NOW).stage.value == "COMPLETE"
        h.coordinator.edge_gate = None
        request = replace(make_request(h.broker, p=proposal(side=Side.SELL, decision_id="exit")),
            edge_evidence=None, projected_factor_positions=())
        sale = h.coordinator.prepare(request)
        assert sale.approved
        assert h.coordinator.execute_due(sale.program.program_id, now=NOW).stage.value == "COMPLETE"
        assert h.broker.get_positions("tenant") == ()
    finally:
        h.close()


@pytest.mark.parametrize("column", ["risk_authority_payload", "risk_authority_version"])
def test_policy_columns_are_append_only(tmp_path, column):
    import sqlite3
    h = Harness(tmp_path)
    try:
        prepared = h.coordinator.prepare(make_request(h.broker))
        before = h.programs.get(prepared.program.program_id)
        with pytest.raises(sqlite3.IntegrityError, match="risk authority is immutable"):
            h.programs.db.execute(f"UPDATE execution_programs SET {column}={column}")
        h.programs.db.rollback()
        assert h.programs.get(prepared.program.program_id) == before
    finally:
        h.close()


@pytest.mark.parametrize("defect", ["missing_payload", "wrong_hash", "wrong_version", "fractional_version", "other_program", "other_tenant", "unknown_engine"])
def test_corrupt_binding_refuses_before_dispatch(tmp_path, defect):
    import json
    h = Harness(tmp_path)
    try:
        prepared = h.coordinator.prepare(make_request(h.broker))
        db = h.programs.db
        db.execute("DROP TRIGGER execution_program_risk_authority_immutable")
        db.execute("DROP TRIGGER execution_program_approved_identity_immutable")
        if defect == "missing_payload":
            db.execute("UPDATE execution_programs SET risk_authority_payload=NULL")
        elif defect == "wrong_hash":
            db.execute("UPDATE execution_programs SET runtime_context_sha256=?", ("f" * 64,))
        elif defect in {"wrong_version", "fractional_version"}:
            db.execute("UPDATE execution_programs SET risk_authority_version=?", (2 if defect == "wrong_version" else 1.5,))
        else:
            value = json.loads(prepared.program.risk_authority_payload)
            value[{"other_program":"programId","other_tenant":"tenantId","unknown_engine":"engine"}[defect]] = "other"
            db.execute("UPDATE execution_programs SET risk_authority_payload=?", (json.dumps(value,sort_keys=True,separators=(",", ":")),))
        db.commit()
        with pytest.raises(ValueError, match="execution_risk_authority"):
            h.coordinator.execute_due(prepared.program.program_id, now=NOW)
        assert h.broker.ledger_entries("tenant") == ()
        assert h.oms.all_orders("tenant") == ()
    finally:
        h.close()


def test_legacy_entry_can_be_inspected_but_cannot_gain_a_new_policy_implicitly(tmp_path):
    from quant_ai.execution.institutional import request_fingerprint
    h = Harness(tmp_path)
    try:
        request = make_request(h.broker)
        risk = h.coordinator.warden.evaluate(request.proposal, request.capital_plan,
                                            request.portfolio, tenant_id="tenant", now=NOW)
        parent = replace(risk.order, strategy_id=request.strategy_id)
        from quant_ai.orders.intent import canonical_order_intent
        plan = h.coordinator.execution_planner.plan(parent, algorithm=request.execution_algorithm,
            constraints=request.execution_constraints, buckets=request.volume_buckets, source="synthetic legacy")
        old = h.programs.create(program_id="legacy-program", tenant_id="tenant",
            decision_id=request.proposal.decision_id, symbol=parent.symbol, plan=plan,
            runtime_context_sha256=request_fingerprint(request), created_at=NOW,
            parent_order_payload=canonical_order_intent(parent))
        assert old.risk_authority_version == 0 and old.risk_authority_payload is None
        h.coordinator.bind_runtime_context(old.program_id, request=request, parent_order=parent)
        result = h.coordinator.execute_due(old.program_id, now=NOW)
        assert result.reason == "execution_risk_policy_binding_missing"
        assert result.program.slices[0].state.value == "PENDING"
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        h.close()


def test_concurrent_different_policies_cannot_claim_same_decision(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from quant_ai.execution.program import ExecutionProgramJournal
    h = Harness(tmp_path)
    other_store = ExecutionProgramJournal(tmp_path / "programs.sqlite")
    try:
        other = _new_coordinator(h, other_store, CalibratedEdgeGate(EdgePolicy(max_risk_fraction=D(".03"))))
        request = make_request(h.broker)
        def run(coordinator):
            try:
                return coordinator.prepare(request).approved
            except ValueError as error:
                return str(error)
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(run, (h.coordinator, other)))
        assert results.count(True) == 1
        assert results.count("execution_program_decision_payload_mismatch") == 1
        assert h.programs.db.execute("SELECT COUNT(*) FROM execution_programs").fetchone()[0] == 1
        assert h.programs.db.execute("SELECT COUNT(*) FROM execution_program_slices").fetchone()[0] == 1
    finally:
        other_store.close()
        h.close()


def test_create_failure_rolls_back_policy_parent_and_slices_together(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    try:
        def failed(*args):
            raise RuntimeError("synthetic interruption before commit")
        monkeypatch.setattr(h.programs, "get", failed)
        with pytest.raises(RuntimeError, match="synthetic interruption"):
            h.coordinator.prepare(make_request(h.broker))
        for table in ("execution_programs", "execution_program_slices"):
            assert h.programs.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    finally:
        h.close()


@pytest.mark.parametrize("field,value", [("max_risk_fraction", True), ("kelly_fraction", "NaN"),
    ("min_resolved_samples", True), ("max_calibration_error", "1.01"),
    ("max_probability_uncertainty", "-0.1"), ("min_conservative_edge", "Infinity"),
    ("unknown", "0.1")])
def test_malformed_serialized_policy_refuses(tmp_path, field, value):
    import json

    from quant_ai.execution.risk_authority import parse_authority
    h = Harness(tmp_path)
    try:
        prepared = h.coordinator.prepare(make_request(h.broker))
        payload = json.loads(prepared.program.risk_authority_payload)
        payload["policy"][field] = value
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with pytest.raises((ValueError, TypeError), match="execution_risk_"):
            parse_authority(raw)
    finally:
        h.close()


def test_duplicate_policy_keys_and_noncanonical_records_refuse(tmp_path):
    from quant_ai.execution.risk_authority import parse_authority
    h = Harness(tmp_path)
    try:
        raw = h.coordinator.prepare(make_request(h.broker)).program.risk_authority_payload
        for malformed in (raw.replace('"min_resolved_samples":30', '"min_resolved_samples":30,"min_resolved_samples":30'), " " + raw):
            with pytest.raises(ValueError, match="execution_risk_authority"):
                parse_authority(malformed)
    finally:
        h.close()


def test_offline_bundle_preserves_policy_and_checks_receipt_binding(tmp_path):
    import json
    import sqlite3

    from test_institutional_state_bundle import fixture, select

    from quant_ai.operations import recovery_bundle as bundle
    spec, _ = fixture(tmp_path)
    select(spec)
    first = bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    restored = bundle.restore(tmp_path / "backup", tmp_path / "restored", manifest_sha256=first["manifestSha256"])
    assert restored["status"] == "restored" and not restored["institutionalRecovery"]["activationAuthorized"]
    with sqlite3.connect(tmp_path / "restored/programs") as db:
        version, raw = db.execute("SELECT risk_authority_version,risk_authority_payload FROM execution_programs").fetchone()
    assert version == 1 and json.loads(raw)["mode"] == "ENTRY"
    with sqlite3.connect(spec["sources"]["ledger"]) as db:
        row = db.execute("SELECT order_id,payload FROM paper_decision_evidence").fetchone()
        value = json.loads(row[1]); value["institutional_risk_authority_sha256"] = "e" * 64
        # The fixture alone may remove append-only guards in order to test tamper rejection.
        triggers = db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='paper_decision_evidence'").fetchall()
        for (name,) in triggers:
            db.execute('DROP TRIGGER "' + name.replace('"','""') + '"')
        db.execute("UPDATE paper_decision_evidence SET payload=? WHERE order_id=?", (json.dumps(value), row[0]))
    with pytest.raises(ValueError, match="receipt risk authority mismatch"):
        bundle.create(spec, tmp_path / "bad-backup", writers_stopped=True)
    assert not (tmp_path / "bad-backup").exists()



def test_policy_change_during_preparation_cannot_label_old_assessment_as_new_policy(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    try:
        original = h.coordinator.warden.evaluate
        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            h.coordinator.edge_gate = CalibratedEdgeGate(EdgePolicy(max_risk_fraction=D(".03")))
            return result
        monkeypatch.setattr(h.coordinator.warden, "evaluate", changed)
        result = h.coordinator.prepare(make_request(h.broker))
        assert not result.approved and result.reason == "execution_risk_policy_changed"
        assert h.programs.db.execute("SELECT COUNT(*) FROM execution_programs").fetchone()[0] == 0
    finally:
        h.close()


@pytest.mark.parametrize("committed", [False, True])
def test_abrupt_process_exit_never_splits_parent_from_policy(tmp_path, committed):
    import json
    import os
    import subprocess
    import sys

    from quant_ai.execution.program import ExecutionProgramJournal
    code = """
import os, sys
from pathlib import Path
from test_institutional_paper_coordinator import Harness, make_request
h = Harness(Path(sys.argv[1]))
if sys.argv[2] == 'uncommitted':
    h.programs.get = lambda *_: os._exit(73)
h.coordinator.prepare(make_request(h.broker))
os._exit(74)
"""
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path),
        "committed" if committed else "uncommitted"], env={**os.environ,
        "TRADING_LIVE_MONEY_ACTIVE": "false"}, capture_output=True, timeout=30, check=False)
    assert result.returncode == (74 if committed else 73), result.stderr.decode()
    with ExecutionProgramJournal(tmp_path / "programs.sqlite") as journal:
        rows = journal.db.execute("SELECT program_id FROM execution_programs").fetchall()
        assert len(rows) == int(committed)
        assert journal.db.execute("SELECT COUNT(*) FROM execution_program_slices").fetchone()[0] == int(committed)
        if committed:
            program = journal.get(rows[0][0])
            assert program.risk_authority_version == 1
            assert json.loads(program.risk_authority_payload)["policy"]["max_risk_fraction"] == "0.02"
            assert program.slices[0].state.value == "PENDING"


def test_independent_protective_exit_is_available_while_new_program_has_policy_hold(tmp_path):
    from test_institutional_paper_coordinator import proposal

    from quant_ai.execution.protective_exits import ProtectiveExitEngine
    h = Harness(tmp_path)
    try:
        entry = h.coordinator.prepare(make_request(h.broker))
        assert h.coordinator.execute_due(entry.program.program_id, now=NOW).stage.value == "COMPLETE"
        request = make_request(h.broker, p=proposal(decision_id="second"))
        request = replace(request, projected_factor_positions=h.factor_provider(request.proposal, request.portfolio))
        second = h.coordinator.prepare(request)
        assert second.approved
        h.coordinator.edge_gate = CalibratedEdgeGate(EdgePolicy(max_risk_fraction=D(".03")))
        assert h.coordinator.execute_due(second.program.program_id, now=NOW).reason == "execution_risk_policy_changed"
        exits = ProtectiveExitEngine(h.broker, lambda _: D(90), tenant_id="tenant").evaluate()
        assert len(exits) == 1 and exits[0].filled
        assert h.broker.get_positions("tenant") == ()
        assert h.programs.get(second.program.program_id).slices[0].state.value == "PENDING"
    finally:
        h.close()



def test_version_one_cannot_drop_parent_snapshot_at_insert(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    try:
        create = h.programs.create
        def inconsistent(**kwargs):
            kwargs["parent_order_payload"] = None
            return create(**kwargs)
        monkeypatch.setattr(h.programs, "create", inconsistent)
        with pytest.raises(ValueError, match="execution_risk_authority_binding_mismatch"):
            h.coordinator.prepare(make_request(h.broker))
        assert h.programs.db.execute("SELECT COUNT(*) FROM execution_programs").fetchone()[0] == 0
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        h.close()
