"""Synthetic saved-request/plan recovery, not live execution or risk-capacity release."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from test_institutional_paper_coordinator import NOW, Harness, make_request, proposal

from quant_ai.execution.institutional import request_fingerprint
from quant_ai.execution.planner import ExecutionAlgorithm, VolumeBucket


def test_new_program_retains_complete_request_and_plan(tmp_path):
    h = Harness(tmp_path)
    try:
        request = make_request(h.broker)
        prepared = h.coordinator.prepare(request)
        stored = h.programs.load_context(prepared.program.program_id, tenant_id="tenant")
        assert request_fingerprint(stored.request) == request_fingerprint(request)
        assert stored.plan == prepared.execution_plan
        assert stored.request.edge_evidence.source_sha256 == request.edge_evidence.source_sha256
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        h.close()


def test_restart_loads_saved_input_without_original_caller_or_duplicate_slice(tmp_path):
    h = Harness(tmp_path)
    prepared = h.coordinator.prepare(make_request(h.broker, p=proposal(quantity=20),
        algorithm=ExecutionAlgorithm.TWAP,
        buckets=(VolumeBucket(NOW, 1000), VolumeBucket(NOW + timedelta(minutes=10), 1000))))
    pid = prepared.program.program_id
    assert h.coordinator.execute_due(pid, now=NOW).executed_sequences == (1,)
    h.close()
    restarted = Harness(tmp_path)
    try:
        before = tuple(restarted.broker._connection.iterdump())
        restored = restarted.coordinator.restore_runtime_context(pid, tenant_id="tenant")
        assert restored.request.proposal.quantity == 20
        assert tuple(restarted.broker._connection.iterdump()) == before
        assert restarted.coordinator.execute_due(pid, now=NOW).executed_sequences == ()
        result = restarted.coordinator.execute_due(pid, now=NOW + timedelta(minutes=10))
        assert result.executed_sequences == (2,)
        assert len(restarted.broker.ledger_entries("tenant")) == 2
    finally:
        restarted.close()


def test_mutating_caller_inputs_does_not_change_saved_or_bound_request(tmp_path):
    h = Harness(tmp_path)
    try:
        request = make_request(h.broker)
        prepared = h.coordinator.prepare(request)
        before = request_fingerprint(request)
        request.factor_policy.factor_caps["TECH"] = Decimal(".00001")
        stored = h.programs.load_context(prepared.program.program_id, tenant_id="tenant")
        assert request_fingerprint(stored.request) == before
        assert request_fingerprint(h.coordinator._requests[prepared.program.program_id]) == before
    finally:
        h.close()


def _raw(h, pid):
    return h.programs.db.execute("SELECT context_payload FROM execution_programs WHERE program_id=?", (pid,)).fetchone()[0]


def _rewrite(h, pid, transform):
    import json
    raw = json.loads(_raw(h, pid))
    transform(raw)
    h.programs.db.execute("DROP TRIGGER IF EXISTS execution_program_context_immutable")
    import hashlib
    updated = json.dumps(raw, sort_keys=True, separators=(",", ":"))
    h.programs.db.execute("UPDATE execution_programs SET context_payload=?,context_sha256=? WHERE program_id=?",
        (updated, hashlib.sha256(updated.encode()).hexdigest(), pid))
    h.programs.db.commit()


@pytest.mark.parametrize("algorithm", list(ExecutionAlgorithm))
def test_every_existing_planner_algorithm_round_trips_supplied_liquidity(tmp_path, algorithm):
    h = Harness(tmp_path)
    try:
        request = make_request(h.broker, algorithm=algorithm,
            buckets=(VolumeBucket(NOW, 1000), VolumeBucket(NOW + timedelta(minutes=10), 500)))
        prepared = h.coordinator.prepare(request)
        assert prepared.approved
        stored = h.programs.load_context(prepared.program.program_id, tenant_id="tenant")
        assert stored.plan == prepared.execution_plan
        assert stored.request.volume_buckets == request.volume_buckets
        assert stored.plan.slices[0].observed_available_quantity == 1000
    finally:
        h.close()


def test_bound_instrument_and_typed_maps_and_provenance_round_trip(tmp_path):
    from datetime import date, timezone

    from quant_ai.agents.swarm import InstrumentBoundTradeProposal
    from quant_ai.domain.models import AssetClass, Instrument, Market
    h = Harness(tmp_path)
    try:
        instrument = Instrument("INFY", Market.INDIA, AssetClass.EQUITY, "INR", "NSE",
                                metadata={"feed": "synthetic", "feature_version": "v-test"})
        provenance = {"at": NOW, "day": date(2026, 9, 15), "probability": 0.7,
                      "tuple": (True, None, Decimal("0.1000")), "list": [1, "one"]}
        bound = InstrumentBoundTradeProposal(**{**vars(proposal()), "provenance": provenance}, instrument=instrument)
        request = replace(make_request(h.broker, p=bound),
            strategy_correlations={("a", "b"): Decimal(".25")},
            observed_at=NOW.astimezone(timezone(timedelta(hours=5, minutes=30))))
        prepared = h.coordinator.prepare(request)
        assert prepared.approved
        stored = h.programs.load_context(prepared.program.program_id, tenant_id="tenant")
        assert type(stored.request.proposal) is InstrumentBoundTradeProposal
        assert stored.request.proposal.instrument == instrument
        assert type(stored.request.proposal.provenance["probability"]) is float
        assert stored.request.proposal.provenance == provenance
        assert stored.request.strategy_correlations == request.strategy_correlations
        assert request_fingerprint(stored.request) == request_fingerprint(request)
        assert "feature_version" in _raw(h, prepared.program.program_id)
    finally:
        h.close()


@pytest.mark.parametrize("part", ["request", "plan"])
def test_payload_changes_cannot_reuse_original_approved_hash(tmp_path, part):
    h = Harness(tmp_path)
    try:
        prepared = h.coordinator.prepare(make_request(h.broker))
        pid = prepared.program.program_id
        def change(raw):
            fields = raw[part][2]
            if part == "request":
                fields["proposal"][2]["rationale"] = ["tuple", [["str", "changed rationale"]]]
            else:
                fields["source"] = ["str", "changed volume source"]
        _rewrite(h, pid, change)
        before = tuple(h.broker._connection.iterdump())
        with pytest.raises(ValueError, match="execution_context_.*digest_mismatch"):
            h.programs.load_context(pid, tenant_id="tenant")
        assert tuple(h.broker._connection.iterdump()) == before
    finally:
        h.close()


@pytest.mark.parametrize("defect", ["schema", "root_extra", "duplicate_json", "unknown_type",
    "unknown_record_field", "wrong_quantity_type", "numeric_decimal", "nonfinite", "naive_time",
    "missing_field", "noncanonical", "duplicate_map_key"])
def test_malformed_context_refuses_without_loading_code_or_mutating_state(tmp_path, defect):
    import json

    from quant_ai.execution.request_context import decode_context
    h = Harness(tmp_path)
    try:
        prepared = h.coordinator.prepare(make_request(h.broker))
        source = _raw(h, prepared.program.program_id)
        value = json.loads(source)
        r = value["request"][2]
        if defect == "schema": value["schema"] = "unrecognized.v7"
        elif defect == "root_extra": value["executable"] = "never imported"
        elif defect == "unknown_type": value["request"][1] = "os.system"
        elif defect == "unknown_record_field": r["unexpected"] = ["str", "new"]
        elif defect == "wrong_quantity_type": r["proposal"][2]["quantity"] = ["bool", True]
        elif defect == "numeric_decimal": r["base_rate"] = ["decimal", 1]
        elif defect == "nonfinite": r["base_rate"] = ["decimal", "NaN"]
        elif defect == "naive_time": r["observed_at"] = ["datetime", "2026-09-16T10:00:00"]
        elif defect == "missing_field": del r["volume_buckets"]
        elif defect == "duplicate_map_key":
            r["current_strategy_weights"] = ["map", [[["str", "a"], ["decimal", "0"]], [["str", "a"], ["decimal", "1"]]]]
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"))
        if defect == "duplicate_json": raw = raw[:-1] + ',"schema":"duplicate"}'
        if defect == "noncanonical": raw += " "
        before = tuple(h.programs.db.iterdump())
        with pytest.raises(ValueError, match="execution_context_"):
            decode_context(raw)
        assert tuple(h.programs.db.iterdump()) == before
    finally:
        h.close()


@pytest.mark.parametrize("column,value", [("context_payload", None), ("context_version", 9),
    ("context_version", 0)])
def test_missing_or_downgraded_context_is_not_silently_treated_as_legacy(tmp_path, column, value):
    h = Harness(tmp_path)
    try:
        prepared = h.coordinator.prepare(make_request(h.broker))
        h.programs.db.execute("DROP TRIGGER execution_program_context_immutable")
        h.programs.db.execute(f"UPDATE execution_programs SET {column}=?", (value,))
        h.programs.db.commit()
        with pytest.raises(ValueError, match="execution_context_"):
            h.coordinator.execute_due(prepared.program.program_id, now=NOW)
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        h.close()


@pytest.mark.parametrize("column,expression", [("context_payload", "NULL"), ("context_version", "0"), ("context_sha256", "NULL")])
def test_persisted_context_cannot_be_updated(tmp_path, column, expression):
    import sqlite3
    h = Harness(tmp_path)
    try:
        h.coordinator.prepare(make_request(h.broker))
        with pytest.raises(sqlite3.IntegrityError, match="context is immutable"):
            h.programs.db.execute(f"UPDATE execution_programs SET {column}={expression}")
        h.programs.db.rollback()
    finally:
        h.close()


def test_cross_tenant_context_load_refuses_without_memory_binding(tmp_path):
    h = Harness(tmp_path)
    try:
        prepared = h.coordinator.prepare(make_request(h.broker))
        h.coordinator._requests.clear(); h.coordinator._orders.clear()
        with pytest.raises(KeyError, match="context_not_found"):
            h.coordinator.restore_runtime_context(prepared.program.program_id, tenant_id="other")
        assert not h.coordinator._requests and not h.coordinator._orders
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        h.close()


def test_legacy_writer_remains_readable_without_fabricated_context(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    try:
        create = h.programs.create
        def legacy(**kwargs):
            kwargs.pop("context_payload", None)
            return create(**kwargs)
        monkeypatch.setattr(h.programs, "create", legacy)
        prepared = h.coordinator.prepare(make_request(h.broker))
        assert h.programs.get(prepared.program.program_id).context_version == 0
        with pytest.raises(ValueError, match="execution_context_legacy_unavailable"):
            h.coordinator.restore_runtime_context(prepared.program.program_id, tenant_id="tenant")
        assert h.programs.get(prepared.program.program_id).context_payload is None
    finally:
        h.close()


def test_context_v1_cannot_masquerade_as_old_writer_without_authority(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    try:
        create = h.programs.create
        def inconsistent(**kwargs):
            kwargs.pop("risk_authority_payload", None)
            return create(**kwargs)
        monkeypatch.setattr(h.programs, "create", inconsistent)
        with pytest.raises(ValueError, match="authority_payload_invalid"):
            h.coordinator.prepare(make_request(h.broker))
        assert h.programs.db.execute("SELECT count(*) FROM execution_programs").fetchone()[0] == 0
    finally:
        h.close()


def test_mutating_restored_result_cannot_change_bound_runtime_memory(tmp_path):
    h = Harness(tmp_path)
    try:
        prepared = h.coordinator.prepare(make_request(h.broker))
        stored = h.coordinator.restore_runtime_context(prepared.program.program_id, tenant_id="tenant")
        stored.request.factor_policy.factor_caps["TECH"] = Decimal(".000001")
        assert h.coordinator.execute_due(prepared.program.program_id, now=NOW).stage.value == "COMPLETE"
        assert h.programs.load_context(prepared.program.program_id, tenant_id="tenant").request.factor_policy.factor_caps["TECH"] == Decimal(".30")
    finally:
        h.close()


def test_rehydration_does_not_override_policy_drift_or_fresh_risk_checks(tmp_path):
    from quant_ai.decision.edge import CalibratedEdgeGate
    h = Harness(tmp_path)
    try:
        prepared = h.coordinator.prepare(make_request(h.broker))
        h.coordinator._requests.clear(); h.coordinator._orders.clear()
        h.coordinator.restore_runtime_context(prepared.program.program_id, tenant_id="tenant")
        h.coordinator.edge_gate = CalibratedEdgeGate(replace(h.coordinator.edge_gate.policy,
                                                         max_risk_fraction=Decimal(".001")))
        result = h.coordinator.execute_due(prepared.program.program_id, now=NOW)
        assert result.reason == "execution_risk_policy_changed"
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        h.close()


def test_input_change_during_preparation_does_not_get_approved_as_original(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    try:
        request = make_request(h.broker)
        plan = h.coordinator.execution_planner.plan
        def changed(*args, **kwargs):
            result = plan(*args, **kwargs)
            request.current_strategy_weights["atlas-strategy"] = Decimal(".1")
            return result
        monkeypatch.setattr(h.coordinator.execution_planner, "plan", changed)
        result = h.coordinator.prepare(request)
        assert not result.approved and result.reason == "execution_context_request_changed"
        assert h.programs.db.execute("SELECT count(*) FROM execution_programs").fetchone()[0] == 0
    finally:
        h.close()


@pytest.mark.parametrize("limit", ["MAX_BYTES", "MAX_NODES", "MAX_DEPTH"])
def test_snapshot_resource_limits_fail_before_a_program_is_committed(tmp_path, monkeypatch, limit):
    from quant_ai.execution import request_context
    h = Harness(tmp_path)
    try:
        monkeypatch.setattr(request_context, limit, 1)
        result = h.coordinator.prepare(make_request(h.broker))
        assert not result.approved and result.reason.startswith("execution_context_")
        assert h.programs.db.execute("SELECT count(*) FROM execution_programs").fetchone()[0] == 0
    finally:
        h.close()


def test_corrupted_saved_schedule_cannot_be_decoded_with_integer_truncation(tmp_path):
    h = Harness(tmp_path)
    try:
        prepared = h.coordinator.prepare(make_request(h.broker))
        h.programs.db.execute("UPDATE execution_program_slices SET quantity=10.5")
        h.programs.db.commit()
        with pytest.raises(ValueError, match="execution_context_schedule_types_invalid"):
            h.programs.load_context(prepared.program.program_id, tenant_id="tenant")
    finally:
        h.close()


def test_offline_bundle_verifies_stored_context_but_never_authorizes_activation(tmp_path):
    from test_institutional_state_bundle import fixture, select

    from quant_ai.operations import recovery_bundle
    spec, _pid = fixture(tmp_path)
    select(spec)
    manifest = recovery_bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    report = recovery_bundle.restore(tmp_path / "backup", tmp_path / "restore",
                                    manifest_sha256=manifest["manifestSha256"])
    context = report["institutionalRecovery"]["programs"]
    assert context["verifiedStoredContexts"] == 1 and context["legacyMissingContexts"] == 0
    assert context["storedRequestAndPlanVerified"] is True
    assert report["institutionalRecovery"]["activationAuthorized"] is False
    assert report["institutionalRecovery"]["runtimeContextVerified"] is False


@pytest.mark.parametrize("boundary", ["before_commit", "after_commit"])
def test_abrupt_restart_preserves_whole_request_plan_commit_boundary(tmp_path, boundary):
    import os
    import subprocess
    import sys
    from pathlib import Path

    from quant_ai.execution.program import ExecutionProgramJournal
    code = r"""
import os, sys
from pathlib import Path
from test_institutional_paper_coordinator import Harness, make_request
h = Harness(Path(sys.argv[1]))
if sys.argv[2] == 'before_commit':
    original = h.programs.get
    def interrupted(pid):
        if h.programs.db.execute('SELECT context_version FROM execution_programs WHERE program_id=?', (pid,)).fetchone():
            os._exit(81)
        return original(pid)
    h.programs.get = interrupted
h.coordinator.prepare(make_request(h.broker))
os._exit(82)
"""
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(root / "src"), str(root / "tests")]),
           "TRADING_LIVE_MONEY_ACTIVE": "false"}
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path), boundary], env=env,
                            cwd=root, capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == (81 if boundary == "before_commit" else 82), result.stderr
    with ExecutionProgramJournal(tmp_path / "programs.sqlite") as journal:
        rows = journal.db.execute("SELECT program_id FROM execution_programs").fetchall()
        assert len(rows) == (0 if boundary == "before_commit" else 1)
        if rows:
            stored = journal.load_context(rows[0][0], tenant_id="tenant")
            assert stored.request.proposal.quantity == stored.plan.parent_quantity == 10


def test_context_loading_never_calls_market_providers_or_deserializes_model_code(tmp_path, monkeypatch):
    import pickle
    h = Harness(tmp_path)
    try:
        prepared = h.coordinator.prepare(make_request(h.broker))
        def forbidden(*args, **kwargs):
            pytest.fail("Loading saved data cannot call an execution/model/provider path")
        monkeypatch.setattr(pickle, "loads", forbidden)
        monkeypatch.setattr(h.coordinator, "snapshot_provider", forbidden)
        monkeypatch.setattr(h.coordinator, "factor_position_provider", forbidden)
        monkeypatch.setattr(h.broker, "submit_with_evidence", forbidden)
        before = tuple(h.programs.db.iterdump())
        result = h.coordinator.restore_runtime_context(prepared.program.program_id, tenant_id="tenant")
        assert result.plan == prepared.execution_plan
        assert tuple(h.programs.db.iterdump()) == before
    finally:
        h.close()


def test_failed_schedule_insert_rolls_back_parent_and_full_context_together(tmp_path):
    import sqlite3
    h = Harness(tmp_path)
    try:
        h.programs.db.execute("CREATE TRIGGER synthetic_insert_failure BEFORE INSERT ON execution_program_slices BEGIN SELECT RAISE(ABORT,'synthetic slice failure'); END")
        h.programs.db.commit()
        with pytest.raises(sqlite3.IntegrityError, match="synthetic slice failure"):
            h.coordinator.prepare(make_request(h.broker))
        assert h.programs.db.execute("SELECT count(*) FROM execution_programs").fetchone()[0] == 0
        assert h.programs.db.execute("SELECT count(*) FROM execution_program_slices").fetchone()[0] == 0
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        h.close()


def test_concurrent_identical_contexts_retain_one_program_and_one_snapshot(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    h, other = Harness(tmp_path), Harness(tmp_path)
    try:
        request = make_request(h.broker)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda coordinator: coordinator.prepare(request),
                                    (h.coordinator, other.coordinator)))
        assert all(result.approved for result in results)
        assert results[0].program.program_id == results[1].program.program_id
        assert h.programs.db.execute("SELECT count(*) FROM execution_programs").fetchone()[0] == 1
        assert h.programs.load_context(results[0].program.program_id, tenant_id="tenant").plan == results[0].execution_plan
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        other.close(); h.close()


def test_rehashed_plan_still_must_match_saved_volume_input(tmp_path):
    import hashlib
    import json

    from quant_ai.execution.program import ExecutionProgramJournal
    from quant_ai.execution.request_context import decode_context, encode_context
    h = Harness(tmp_path)
    try:
        prepared = h.coordinator.prepare(make_request(h.broker))
        pid = prepared.program.program_id
        stored = decode_context(_raw(h, pid))
        wrong_slice = replace(stored.plan.slices[0], observed_available_quantity=2000,
                              participation=Decimal(".005"))
        wrong_plan = replace(stored.plan, slices=(wrong_slice,))
        raw = encode_context(stored.request, wrong_plan)
        digest = hashlib.sha256(json.dumps(ExecutionProgramJournal._plan_payload(wrong_plan),
                          sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        for name in ("execution_program_context_immutable", "execution_program_approved_identity_immutable"):
            h.programs.db.execute(f'DROP TRIGGER "{name}"')
        h.programs.db.execute("UPDATE execution_programs SET context_payload=?,context_sha256=?,plan_sha256=?",
                             (raw, hashlib.sha256(raw.encode()).hexdigest(), digest))
        h.programs.db.commit()
        with pytest.raises(ValueError, match="execution_context_plan_liquidity_mismatch"):
            h.programs.load_context(pid, tenant_id="tenant")
    finally:
        h.close()


def test_context_corruption_is_not_hidden_by_backup_file_checksums(tmp_path):
    from test_institutional_state_bundle import fixture, mutate, select

    from quant_ai.operations import recovery_bundle
    spec, _ = fixture(tmp_path)
    select(spec)
    mutate(spec["institutional_state"]["programs"], "execution_programs",
           "UPDATE execution_programs SET context_payload=NULL")
    with pytest.raises(ValueError, match="execution_context_version_or_payload_invalid"):
        recovery_bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    assert not (tmp_path / "backup").exists()


def test_typed_metadata_change_cannot_hide_behind_legacy_request_hash(tmp_path):
    import json

    from quant_ai.execution.request_context import decode_context
    h = Harness(tmp_path)
    try:
        request = make_request(h.broker, p=replace(proposal(), provenance={"observed_at": NOW}))
        prepared = h.coordinator.prepare(request)
        pid = prepared.program.program_id
        value = json.loads(_raw(h, pid))
        provenance = value["request"][2]["proposal"][2]["provenance"]
        provenance[1][0][1][0] = "str"  # Same ISO text, different metadata type.
        changed = json.dumps(value, sort_keys=True, separators=(",", ":"))
        assert request_fingerprint(decode_context(changed).request) == request_fingerprint(request)
        h.programs.db.execute("DROP TRIGGER execution_program_context_immutable")
        h.programs.db.execute("UPDATE execution_programs SET context_payload=?", (changed,))
        h.programs.db.commit()
        with pytest.raises(ValueError, match="execution_context_payload_digest_mismatch"):
            h.programs.load_context(pid, tenant_id="tenant")
    finally:
        h.close()


def test_caller_mutation_holds_until_explicit_saved_context_rebind(tmp_path):
    h = Harness(tmp_path)
    try:
        request = make_request(h.broker)
        prepared = h.coordinator.prepare(request)
        request.current_strategy_weights["changed-after-approval"] = Decimal(".5")
        with pytest.raises(ValueError, match="execution_program_runtime_context_mismatch"):
            h.coordinator.execute_due(prepared.program.program_id, now=NOW)
        assert h.broker.ledger_entries("tenant") == ()
        h.coordinator.restore_runtime_context(prepared.program.program_id, tenant_id="tenant")
        assert h.coordinator.execute_due(prepared.program.program_id, now=NOW).stage.value == "COMPLETE"
    finally:
        h.close()
