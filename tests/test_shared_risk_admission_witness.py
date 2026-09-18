"""Synthetic pending-reservation rollback checks; no capacity-release implementation."""
import sqlite3
from dataclasses import replace
from decimal import Decimal

import pytest
from test_institutional_paper_coordinator import NOW, Harness, make_request, proposal
from test_shared_risk_reservations import enable, policy

from quant_ai.execution.shared_risk_binding import verify_binding_pair
from quant_ai.orders.oms import DurableOms

D = Decimal


def pending_rollback(tmp_path):
    h = Harness(tmp_path)
    enable(h)
    initial = h.coordinator.prepare(make_request(h.broker, p=proposal(decision_id="initial")))
    assert initial.approved
    h.coordinator.shared_risk.cancel_unclaimed(initial.program.program_id, tenant_id="tenant", at=NOW)
    old = sqlite3.connect(tmp_path / "old-programs.sqlite")
    h.programs.db.backup(old)
    pending = h.coordinator.prepare(make_request(h.broker, p=proposal(decision_id="pending")))
    assert pending.approved
    old.backup(h.programs.db)
    old.close()
    assert h.broker.ledger_entries("tenant") == ()
    return h, pending


def test_pending_reservation_rollback_cannot_readmit_same_capacity(tmp_path):
    h, _ = pending_rollback(tmp_path)
    try:
        before = tuple(h.programs.db.iterdump())
        result = h.coordinator.prepare(make_request(h.broker, p=proposal(decision_id="after-rollback")))
        assert not result.approved and "admission_witness" in result.reason
        assert tuple(h.programs.db.iterdump()) == before
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        h.close()


def test_pairing_requires_unexecuted_reservation_history(tmp_path):
    h, _ = pending_rollback(tmp_path)
    try:
        with pytest.raises(ValueError, match="admission_witness"):
            verify_binding_pair(h.broker._connection, h.programs.db, "tenant")
    finally:
        h.close()


def test_final_broker_refuses_lost_other_pending_reservation(tmp_path):
    h = Harness(tmp_path)
    old = sqlite3.connect(tmp_path / "before-second.sqlite")
    try:
        h.coordinator.shared_risk_policy = replace(policy(), max_loss_fraction=D(".001"))
        queued = h.coordinator.prepare(make_request(h.broker, p=proposal(decision_id="queued")))
        assert queued.approved
        h.programs.db.backup(old)
        other = h.coordinator.prepare(make_request(h.broker, p=proposal(decision_id="other-pending")))
        assert other.approved
        old.backup(h.programs.db)
        pid, child = queued.program.program_id, queued.approved_order
        assert h.programs.claim_slice(pid, 1, client_order_id=DurableOms.client_order_id(child, "queued:slice:1"))
        evidence = {"schema": "pramana.swarm_fill.v1", "event_type": "swarm_fill",
            "institutional_program": pid, "institutional_slice": 1,
            "institutional_risk_authority_sha256": queued.program.runtime_context_sha256}
        before = tuple(h.broker._connection.iterdump())
        with pytest.raises(ValueError, match="admission_witness"):
            h.broker.submit_with_evidence(child, evidence, f"{pid}:1")
        assert tuple(h.broker._connection.iterdump()) == before
    finally:
        old.close()
        h.close()


@pytest.mark.parametrize("check_paths", [True, False])
def test_offline_path_exemption_does_not_skip_pending_history(tmp_path, check_paths):
    h, _ = pending_rollback(tmp_path)
    try:
        with pytest.raises(ValueError, match="admission_witness"):
            verify_binding_pair(h.broker._connection, h.programs.db, "tenant", check_paths=check_paths)
    finally:
        h.close()


def test_repeat_decision_and_cancellation_retain_one_immutable_witness(tmp_path):
    from quant_ai.execution.shared_risk_admission import TABLE
    h = Harness(tmp_path)
    try:
        enable(h)
        request = make_request(h.broker)
        first = h.coordinator.prepare(request)
        assert first.approved and h.coordinator.prepare(request).approved
        before = h.broker._connection.execute(f"SELECT * FROM {TABLE}").fetchall()
        assert len(before) == 1
        h.coordinator.shared_risk.cancel_unclaimed(first.program.program_id, tenant_id="tenant", at=NOW)
        assert h.broker._connection.execute(f"SELECT * FROM {TABLE}").fetchall() == before
        assert verify_binding_pair(h.broker._connection, h.programs.db, "tenant") is not None
        assert h.coordinator.prepare(make_request(h.broker, p=proposal(decision_id="new"))).approved
        assert h.broker._connection.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0] == 2
    finally:
        h.close()


def test_capacity_rejection_cannot_leave_an_unadmitted_witness(tmp_path):
    h = Harness(tmp_path)
    try:
        enable(h)
        assert h.coordinator.prepare(make_request(h.broker)).approved
        before = tuple(h.broker._connection.iterdump())
        result = h.coordinator.prepare(make_request(h.broker, p=proposal(decision_id="exceeds")))
        assert not result.approved and result.reason == "shared_risk_budget_exceeded"
        assert tuple(h.broker._connection.iterdump()) == before
    finally:
        h.close()


def test_same_journal_reopen_keeps_pending_witness_and_budget(tmp_path):
    from quant_ai.execution.shared_risk_admission import TABLE
    h = Harness(tmp_path)
    enable(h)
    request = make_request(h.broker)
    prepared = h.coordinator.prepare(request)
    before = [tuple(r) for r in h.broker._connection.execute(f"SELECT * FROM {TABLE}")]
    h.close()
    restarted = Harness(tmp_path)
    try:
        enable(restarted)
        assert verify_binding_pair(restarted.broker._connection, restarted.programs.db, "tenant") is not None
        assert [tuple(r) for r in restarted.broker._connection.execute(f"SELECT * FROM {TABLE}")] == before
        restarted.coordinator.bind_runtime_context(prepared.program.program_id, request=request,
                                                   parent_order=prepared.approved_order)
        assert restarted.coordinator.execute_due(prepared.program.program_id, now=NOW).stage.value == "COMPLETE"
        assert len(restarted.broker.ledger_entries("tenant")) == 1
    finally:
        restarted.close()


def test_competing_new_reservations_write_only_the_admitted_witness(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from quant_ai.execution.shared_risk_admission import TABLE
    first, second = Harness(tmp_path), Harness(tmp_path)
    try:
        enable(first); enable(second)
        barrier = Barrier(2)
        def run(pair):
            h, decision = pair
            request = make_request(h.broker, p=proposal(decision_id=decision))
            barrier.wait(timeout=5)
            return h.coordinator.prepare(request)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, [(first, "thread-a"), (second, "thread-b")]))
        assert sum(r.approved for r in results) == 1
        assert first.broker._connection.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0] == 1
        assert verify_binding_pair(first.broker._connection, first.programs.db, "tenant") is not None
        assert first.broker.ledger_entries("tenant") == ()
    finally:
        second.close(); first.close()


def _corrupt(db, table, sql, args=()):
    # Disposable test database, not a configured/running account.
    for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,)).fetchall():
        assert name.replace("_", "").isalnum()
        db.execute(f'DROP TRIGGER "{name}"')
    db.execute(sql, args)
    db.commit()


@pytest.mark.parametrize("defect", ["table", "row", "hash", "payload", "schema", "other_program", "other_tenant"])
def test_missing_or_altered_broker_witness_holds_new_entries(tmp_path, defect):
    from quant_ai.execution.shared_risk_admission import TABLE
    h = Harness(tmp_path)
    try:
        enable(h)
        assert h.coordinator.prepare(make_request(h.broker)).approved
        ledger = h.broker._connection
        if defect == "table":
            ledger.execute(f"DROP TABLE {TABLE}"); ledger.commit()
        elif defect == "schema":
            ledger.execute(f"ALTER TABLE {TABLE} ADD COLUMN unknown TEXT"); ledger.commit()
        else:
            sql, args = {
                "row": (f"DELETE FROM {TABLE}", ()),
                "hash": (f"UPDATE {TABLE} SET sha256=?", ("b" * 64,)),
                "payload": (f"UPDATE {TABLE} SET payload=?", ("{}",)),
                "other_program": (f"UPDATE {TABLE} SET program_id=?", ("PROGRAM-wrong",)),
                "other_tenant": (f"UPDATE {TABLE} SET tenant_id=?", ("different",)),
            }[defect]
            _corrupt(ledger, TABLE, sql, args)
        before = tuple(ledger.iterdump()), tuple(h.programs.db.iterdump())
        result = h.coordinator.prepare(make_request(h.broker, p=proposal(decision_id="after-corruption")))
        assert not result.approved and "admission_witness" in result.reason
        assert (tuple(ledger.iterdump()), tuple(h.programs.db.iterdump())) == before
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        h.close()


@pytest.mark.parametrize("sql", ["UPDATE {table} SET payload=payload", "DELETE FROM {table}"])
def test_admission_witnesses_are_append_only(tmp_path, sql):
    from quant_ai.execution.shared_risk_admission import TABLE
    h = Harness(tmp_path)
    try:
        enable(h)
        assert h.coordinator.prepare(make_request(h.broker)).approved
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            h.broker._connection.execute(sql.format(table=TABLE))
        h.broker._connection.rollback()
    finally:
        h.close()


@pytest.mark.parametrize("field,value", [("plan_sha256", "b" * 64), ("decision_id", "changed"),
                                         ("created_at", "2026-09-15T10:00:00+00:00")])
def test_changed_pending_program_cannot_keep_its_original_witness(tmp_path, field, value):
    h = Harness(tmp_path)
    try:
        enable(h)
        assert h.coordinator.prepare(make_request(h.broker)).approved
        _corrupt(h.programs.db, "execution_programs", f"UPDATE execution_programs SET {field}=?", (value,))
        with pytest.raises(ValueError, match="admission_witness"):
            verify_binding_pair(h.broker._connection, h.programs.db, "tenant")
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        h.close()


@pytest.mark.parametrize("field,value", [("scheduled_at", "2026-09-16T10:01:00+00:00"),
                                         ("quantity", 9), ("sequence", 2)])
def test_changed_pending_schedule_refuses(tmp_path, field, value):
    h = Harness(tmp_path)
    try:
        enable(h)
        assert h.coordinator.prepare(make_request(h.broker)).approved
        _corrupt(h.programs.db, "execution_program_slices", f"UPDATE execution_program_slices SET {field}=?", (value,))
        with pytest.raises(ValueError, match="admission_witness"):
            verify_binding_pair(h.broker._connection, h.programs.db, "tenant")
    finally:
        h.close()


def test_failed_second_journal_commit_leaves_durable_witness_hold(tmp_path, monkeypatch):
    from quant_ai.execution import institutional
    from quant_ai.execution.shared_risk_admission import TABLE
    h = Harness(tmp_path)
    try:
        enable(h)
        first = h.coordinator.prepare(make_request(h.broker))
        h.coordinator.shared_risk.cancel_unclaimed(first.program.program_id, tenant_id="tenant", at=NOW)
        original = institutional.pin_account
        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("synthetic interruption after broker witness commit")
        monkeypatch.setattr(institutional, "pin_account", interrupted)
        with pytest.raises(RuntimeError, match="synthetic interruption"):
            h.coordinator.prepare(make_request(h.broker, p=proposal(decision_id="interrupted")))
        assert h.programs.db.execute("SELECT count(*) FROM execution_programs").fetchone()[0] == 1
        assert h.broker._connection.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0] == 2
        monkeypatch.setattr(institutional, "pin_account", original)
        retried = h.coordinator.prepare(make_request(h.broker, p=proposal(decision_id="interrupted")))
        assert not retried.approved and "admission_witness" in retried.reason
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        h.close()


def test_protection_does_not_wait_for_pending_reservation_history(tmp_path):
    from quant_ai.execution.protective_exits import ProtectiveExitEngine
    h = Harness(tmp_path)
    old = sqlite3.connect(tmp_path / "before-pending.sqlite")
    try:
        h.coordinator.shared_risk_policy = replace(policy(), max_loss_fraction=D(".001"))
        first = h.coordinator.prepare(make_request(h.broker))
        assert h.coordinator.execute_due(first.program.program_id, now=NOW).stage.value == "COMPLETE"
        h.programs.db.backup(old)
        request = make_request(h.broker, p=proposal(decision_id="pending"))
        request = replace(request, projected_factor_positions=h.factor_provider(request.proposal, request.portfolio))
        assert h.coordinator.prepare(request).approved
        old.backup(h.programs.db)
        with pytest.raises(ValueError, match="admission_witness"):
            verify_binding_pair(h.broker._connection, h.programs.db, "tenant")
        outcomes = ProtectiveExitEngine(h.broker, lambda _: D(90), tenant_id="tenant").evaluate()
        assert len(outcomes) == 1 and outcomes[0].filled
        assert h.broker.get_positions("tenant") == ()
        assert h.coordinator.reconcile_protective_accounting(currency="INR").status == "matched"
    finally:
        old.close(); h.close()


@pytest.mark.parametrize("bound", ["MAX_RECORDS", "MAX_SLICES"])
def test_excessive_witness_inventory_refuses_without_truncation(tmp_path, monkeypatch, bound):
    from quant_ai.execution import shared_risk_admission
    h = Harness(tmp_path)
    try:
        enable(h)
        assert h.coordinator.prepare(make_request(h.broker)).approved
        monkeypatch.setattr(shared_risk_admission, bound, 0)
        with pytest.raises(ValueError, match="admission_witness_inventory_limit"):
            verify_binding_pair(h.broker._connection, h.programs.db, "tenant")
    finally:
        h.close()


def test_unselected_account_has_no_optional_witness_table(tmp_path):
    from quant_ai.execution.shared_risk_admission import TABLE
    h = Harness(tmp_path)
    try:
        assert h.coordinator.prepare(make_request(h.broker)).approved
        assert h.broker._connection.execute("SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,)).fetchone() is None
    finally:
        h.close()



def test_old_v1_binding_is_readable_offline_but_not_silently_upgraded(tmp_path):
    import hashlib
    import json

    from quant_ai.execution.shared_risk_admission import TABLE
    from quant_ai.execution.shared_risk_binding import LEGACY_SCHEMA, read_binding
    h = Harness(tmp_path)
    try:
        enable(h)
        assert h.coordinator.prepare(make_request(h.broker)).approved
        value = read_binding(h.broker._connection, "tenant")
        value["schema"] = LEGACY_SCHEMA
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(raw.encode()).hexdigest()
        _corrupt(h.broker._connection, "paper_shared_risk_bindings",
                 "UPDATE paper_shared_risk_bindings SET payload=?,sha256=?", (raw, digest))
        _corrupt(h.broker._connection, "paper_accounts",
                 "UPDATE paper_accounts SET shared_risk_binding_sha256=?", (digest,))
        h.broker._connection.execute(f"DROP TABLE {TABLE}"); h.broker._connection.commit()
        before = tuple(h.broker._connection.iterdump())
        assert read_binding(h.broker._connection, "tenant")["schema"] == LEGACY_SCHEMA
        assert verify_binding_pair(h.broker._connection, h.programs.db, "tenant", check_paths=False) is not None
        result = h.coordinator.prepare(make_request(h.broker, p=proposal(decision_id="needs-review")))
        assert not result.approved and result.reason == "shared_risk_broker_admission_witness_migration_required"
        assert tuple(h.broker._connection.iterdump()) == before
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        h.close()


@pytest.mark.parametrize("boundary", ["before_witness", "after_witness", "after_commit"])
def test_process_death_preserves_witness_ordering_and_holds_uncertainty(tmp_path, monkeypatch, boundary):
    import os
    import subprocess
    import sys
    from pathlib import Path

    from quant_ai.execution.shared_risk_admission import TABLE
    h = Harness(tmp_path)
    enable(h)
    first = h.coordinator.prepare(make_request(h.broker))
    h.coordinator.shared_risk.cancel_unclaimed(first.program.program_id, tenant_id="tenant", at=NOW)
    h.close()
    root = Path(__file__).resolve().parents[1]
    code = """
import os, sys
from pathlib import Path
from test_institutional_paper_coordinator import Harness, make_request, proposal
from test_shared_risk_reservations import enable
from quant_ai.execution import institutional
h = Harness(Path(sys.argv[1])); enable(h)
original = institutional.pin_account
boundary = sys.argv[2]
def interrupted(*args, **kwargs):
    if boundary == 'before_witness': os._exit(74)
    original(*args, **kwargs)
    if boundary == 'after_witness': os._exit(74)
institutional.pin_account = interrupted
assert h.coordinator.prepare(make_request(h.broker, p=proposal(decision_id='new-child'))).approved
os._exit(74)
"""
    monkeypatch.delenv("PYTHONPATH", raising=False)
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path), boundary],
        env={**os.environ, "PYTHONPATH": os.pathsep.join([str(root / "src"), str(root / "tests")]),
             "TRADING_LIVE_MONEY_ACTIVE": "false"}, cwd=root, capture_output=True, timeout=30, check=False)
    assert result.returncode == 74, result.stderr.decode()
    reopened = Harness(tmp_path)
    try:
        programs = reopened.programs.db.execute("SELECT count(*) FROM execution_programs").fetchone()[0]
        witnesses = reopened.broker._connection.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0]
        assert (programs, witnesses) == {"before_witness": (1, 1), "after_witness": (1, 2),
                                        "after_commit": (2, 2)}[boundary]
        if boundary == "after_witness":
            with pytest.raises(ValueError, match="admission_witness"):
                verify_binding_pair(reopened.broker._connection, reopened.programs.db, "tenant")
        else:
            assert verify_binding_pair(reopened.broker._connection, reopened.programs.db, "tenant") is not None
        assert reopened.broker.ledger_entries("tenant") == ()
    finally:
        reopened.close()


def test_claimed_program_cannot_backfill_a_deleted_witness(tmp_path):
    from quant_ai.execution.shared_risk_admission import TABLE, record_admission
    from quant_ai.execution.shared_risk_binding import read_binding
    h = Harness(tmp_path)
    try:
        enable(h)
        prepared = h.coordinator.prepare(make_request(h.broker))
        pid = prepared.program.program_id
        assert h.programs.claim_slice(pid, 1, client_order_id=DurableOms.client_order_id(prepared.approved_order, "decision-1:slice:1"))
        pin = read_binding(h.broker._connection, "tenant")
        _corrupt(h.broker._connection, TABLE, f"DELETE FROM {TABLE}")
        with h.programs.transaction(), pytest.raises(ValueError, match="cannot_backfill_claimed_program"):
            record_admission(h.broker._connection, h.programs.db, pin, pid)
        assert h.broker._connection.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0] == 0
    finally:
        h.close()


@pytest.mark.parametrize("execute", [True, False])
def test_multistore_backup_preserves_witnesses_without_enabling_orders(tmp_path, monkeypatch, execute):
    from test_institutional_state_bundle import fixture, select

    from quant_ai.execution import institutional
    from quant_ai.execution.shared_risk_admission import TABLE
    from quant_ai.operations import recovery_bundle
    original = institutional.InstitutionalPaperCoordinator.__init__
    def configured(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.shared_risk_policy = policy()
    monkeypatch.setattr(institutional.InstitutionalPaperCoordinator, "__init__", configured)
    spec, _ = fixture(tmp_path, execute=execute)
    select(spec)
    manifest = recovery_bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    report = recovery_bundle.restore(tmp_path / "backup", tmp_path / "restored", manifest_sha256=manifest["manifestSha256"])
    assert report["status"] == ("restored" if execute else "discrepancy")
    assert report["institutionalRecovery"]["activationAuthorized"] is False
    source, restored = sqlite3.connect(spec["sources"]["ledger"]), sqlite3.connect(tmp_path / "restored/ledger")
    try:
        assert source.execute(f"SELECT * FROM {TABLE}").fetchall() == restored.execute(f"SELECT * FROM {TABLE}").fetchall()
    finally:
        source.close(); restored.close()


def test_all_stores_rolled_back_together_is_not_claimed_as_detectable(tmp_path):
    # Explicit scope boundary: local witnesses cannot detect rollback of themselves.
    h = Harness(tmp_path)
    old_journal, old_broker = sqlite3.connect(tmp_path / "old-journal.sqlite"), sqlite3.connect(tmp_path / "old-broker.sqlite")
    try:
        enable(h)
        first = h.coordinator.prepare(make_request(h.broker))
        h.coordinator.shared_risk.cancel_unclaimed(first.program.program_id, tenant_id="tenant", at=NOW)
        h.programs.db.backup(old_journal); h.broker._connection.backup(old_broker)
        assert h.coordinator.prepare(make_request(h.broker, p=proposal(decision_id="later"))).approved
        old_journal.backup(h.programs.db); old_broker.backup(h.broker._connection)
        assert verify_binding_pair(h.broker._connection, h.programs.db, "tenant") is not None
    finally:
        old_journal.close(); old_broker.close(); h.close()
