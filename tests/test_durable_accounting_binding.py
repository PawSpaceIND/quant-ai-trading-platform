"""Disposable paper stores: restart must not silently redirect accounting."""
from decimal import Decimal

import pytest
from test_institutional_paper_coordinator import NOW, Harness, make_request, proposal

from quant_ai.accounting.journal import TradingJournal
from quant_ai.accounting.trading import TradingAccounting
from quant_ai.execution.institutional import InstitutionalPaperCoordinator


def restarted(h, journal, programs=None):
    old = h.coordinator
    return InstitutionalPaperCoordinator(broker=h.broker, oms=h.oms, programs=programs or h.programs,
        accounting=TradingAccounting(journal, "tenant"), warden=old.warden,
        snapshot_provider=old.snapshot_provider, factor_position_provider=old.factor_position_provider,
        strategy_exposure_provider=old.strategy_exposure_provider,
        slice_volume_provider=old.slice_volume_provider, edge_gate=old.edge_gate,
        shared_risk_policy=old.shared_risk_policy)


def replacement(tmp_path):
    journal = TradingJournal(tmp_path / "different.sqlite", base_currency="INR")
    TradingAccounting(journal, "tenant").seed_capital("capital", currency="INR",
        amount=Decimal(100000), base_amount=Decimal(100000), at=NOW)
    return journal


def test_restarted_coordinator_refuses_same_label_different_accounting_database(tmp_path):
    h = Harness(tmp_path)
    other = replacement(tmp_path)
    try:
        prepared = h.coordinator.prepare(make_request(h.broker))
        coordinator = restarted(h, other)
        with pytest.raises(ValueError, match="institutional_accounting_binding_mismatch"):
            coordinator.restore_runtime_context(prepared.program.program_id, tenant_id="tenant")
        assert h.broker.ledger_entries("tenant") == ()
        assert other.db.execute("SELECT count(*) FROM trading_transactions WHERE kind='TRADE'").fetchone()[0] == 0
    finally:
        other.close(); h.close()


def test_new_program_cannot_silently_move_existing_tenant_accounting_after_restart(tmp_path):
    h = Harness(tmp_path)
    other = replacement(tmp_path)
    try:
        first = h.coordinator.prepare(make_request(h.broker))
        assert first.approved
        coordinator = restarted(h, other)
        result = coordinator.prepare(make_request(h.broker, p=proposal(decision_id="next")))
        assert not result.approved
        assert result.reason == "institutional_accounting_binding_mismatch"
        assert h.programs.db.execute("SELECT count(*) FROM execution_programs").fetchone()[0] == 1
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        other.close(); h.close()


def test_same_persisted_store_reopens_and_executes_exactly_once(tmp_path):
    h = Harness(tmp_path)
    reopened = None
    try:
        prepared = h.coordinator.prepare(make_request(h.broker))
        raw = prepared.program.accounting_scope_payload
        assert raw and h.broker.ledger_entries("tenant") == ()
        h.journal.close()
        reopened = TradingJournal(tmp_path / "accounting.sqlite", base_currency="INR")
        coordinator = restarted(h, reopened)
        coordinator.restore_runtime_context(prepared.program.program_id, tenant_id="tenant")
        assert h.broker.ledger_entries("tenant") == ()
        assert coordinator.execute_due(prepared.program.program_id, now=NOW).stage.value == "COMPLETE"
        assert coordinator.execute_due(prepared.program.program_id, now=NOW).executed_sequences == ()
        assert len(h.broker.ledger_entries("tenant")) == 1
        assert reopened.native_balance("tenant", "CASH_AVAILABLE", "INR") == Decimal(99000)
        assert h.programs.get(prepared.program.program_id).accounting_scope_payload == raw
    finally:
        if reopened: reopened.close()
        h.close()


def test_wrong_store_refusal_does_not_provision_identity_or_touch_economics(tmp_path):
    h = Harness(tmp_path)
    other = replacement(tmp_path)
    try:
        h.coordinator.prepare(make_request(h.broker))
        before = tuple(other.db.iterdump())
        result = restarted(h, other).prepare(make_request(h.broker, p=proposal(decision_id="wrong")))
        assert not result.approved and result.reason == "institutional_accounting_binding_mismatch"
        assert tuple(other.db.iterdump()) == before
    finally:
        other.close(); h.close()


def test_identical_copy_at_another_path_requires_runtime_migration_but_can_be_verified_offline(tmp_path):
    import sqlite3

    from quant_ai.execution.accounting_binding import verify_connection
    h = Harness(tmp_path)
    copied = None
    try:
        prepared = h.coordinator.prepare(make_request(h.broker))
        target = sqlite3.connect(tmp_path / "copied.sqlite")
        h.journal.db.backup(target); target.close()
        copied = TradingJournal(tmp_path / "copied.sqlite", base_currency="INR")
        before = tuple(copied.db.iterdump())
        with pytest.raises(ValueError, match="institutional_accounting_binding_mismatch"):
            restarted(h, copied).restore_runtime_context(prepared.program.program_id, tenant_id="tenant")
        assert verify_connection(prepared.program.accounting_scope_payload, copied.db,
                                 "tenant", check_paths=False)
        assert tuple(copied.db.iterdump()) == before
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        if copied: copied.close()
        h.close()


def test_same_path_replacement_with_another_store_cannot_reuse_programme(tmp_path):
    import shutil

    from quant_ai.execution.accounting_binding import select_binding
    h = Harness(tmp_path)
    other = replacement(tmp_path)
    reopened = None
    try:
        prepared = h.coordinator.prepare(make_request(h.broker))
        select_binding(other, "tenant")
        h.journal.close(); other.close()
        (tmp_path / "accounting.sqlite").rename(tmp_path / "original-retained.sqlite")
        shutil.copyfile(tmp_path / "different.sqlite", tmp_path / "accounting.sqlite")
        reopened = TradingJournal(tmp_path / "accounting.sqlite", base_currency="INR")
        with pytest.raises(ValueError, match="institutional_accounting_binding_mismatch"):
            restarted(h, reopened).restore_runtime_context(prepared.program.program_id, tenant_id="tenant")
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        if reopened: reopened.close()
        other.close(); h.close()


@pytest.mark.parametrize("defect", ["uuid", "missing", "currency"])
def test_changed_store_metadata_holds_without_repairing_it(tmp_path, defect):
    h = Harness(tmp_path)
    try:
        prepared = h.coordinator.prepare(make_request(h.broker))
        h.journal.db.execute("DROP TRIGGER trading_journal_store_identity_immutable")
        if defect == "uuid":
            h.journal.db.execute("UPDATE trading_journal_meta SET store_identity=?", ("f" * 32,))
        elif defect == "missing":
            h.journal.db.execute("UPDATE trading_journal_meta SET store_identity=NULL")
        else:
            h.journal.db.execute("UPDATE trading_journal_meta SET base_currency='USD'")
        h.journal.db.commit()
        before = tuple(h.journal.db.iterdump())
        with pytest.raises(ValueError, match="institutional_accounting_binding_mismatch"):
            h.coordinator.execute_due(prepared.program.program_id, now=NOW)
        assert tuple(h.journal.db.iterdump()) == before
        assert h.broker.ledger_entries("tenant") == ()
        assert h.programs.get(prepared.program.program_id).slices[0].state.value == "PENDING"
    finally:
        h.close()


@pytest.mark.parametrize("statement,message", [
    ("UPDATE trading_journal_meta SET store_identity='changed'", "identity is immutable"),
    ("DELETE FROM trading_journal_meta", "identity is retained"),
])
def test_store_identity_is_retained_immutable(tmp_path, statement, message):
    import sqlite3
    h = Harness(tmp_path)
    try:
        h.coordinator.prepare(make_request(h.broker))
        with pytest.raises(sqlite3.IntegrityError, match=message):
            h.journal.db.execute(statement)
        h.journal.db.rollback()
    finally:
        h.close()


def test_programme_accounting_selection_is_immutable(tmp_path):
    import sqlite3
    h = Harness(tmp_path)
    try:
        h.coordinator.prepare(make_request(h.broker))
        with pytest.raises(sqlite3.IntegrityError, match="Accounting scope is immutable"):
            h.programs.db.execute("UPDATE execution_programs SET accounting_scope_payload=NULL")
        h.programs.db.rollback()
    finally:
        h.close()


@pytest.mark.parametrize("defect", ["schema", "tenant", "identity", "currency", "relative_path",
    "extra", "duplicate", "noncanonical", "not_object", "type"])
def test_malformed_binding_refuses_before_any_execution(tmp_path, defect):
    import json

    from quant_ai.execution.accounting_binding import validate_binding
    h = Harness(tmp_path)
    try:
        prepared = h.coordinator.prepare(make_request(h.broker))
        value = json.loads(prepared.program.accounting_scope_payload)
        changes = {"schema": {"schema": "wrong"}, "tenant": {"tenant": "other"},
            "identity": {"journalId": "short"}, "currency": {"baseCurrency": "inr"},
            "relative_path": {"storagePath": "../outside.sqlite"}, "extra": {"extra": "x"},
            "type": {"journalId": True}}
        value.update(changes.get(defect, {}))
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"))
        if defect == "duplicate": raw = '{"schema":"wrong",' + raw[1:]
        if defect == "noncanonical": raw += " "
        if defect == "not_object": raw = "[]"
        with pytest.raises(ValueError, match="institutional_accounting_binding_mismatch"):
            validate_binding(raw, "tenant")
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        h.close()


def test_offline_backup_checks_store_identity_even_when_both_journals_balance(tmp_path):
    import sqlite3

    from test_institutional_state_bundle import fixture, select

    from quant_ai.operations import recovery_bundle
    spec, _pid = fixture(tmp_path)
    select(spec)
    other = replacement(tmp_path)
    try:
        # Clone its accounting rows to preserve exactly the same economics, but
        # provision another independent identity in a separate database.
        original = sqlite3.connect(spec["institutional_state"]["accounting"])
        original.backup(other.db); original.close()
        other.db.execute("DROP TRIGGER trading_journal_store_identity_immutable")
        other.db.execute("UPDATE trading_journal_meta SET store_identity=?", ("e" * 32,))
        other.db.commit()
        spec["institutional_state"]["accounting"] = str(other.path)
        with pytest.raises(ValueError, match="institutional_accounting_binding_mismatch"):
            recovery_bundle.create(spec, tmp_path / "bad-backup", writers_stopped=True)
        assert not (tmp_path / "bad-backup").exists()
    finally:
        other.close()


def test_good_backup_reports_binding_and_preserves_it_without_runtime_activation(tmp_path):
    from test_institutional_state_bundle import fixture, select

    from quant_ai.operations import recovery_bundle
    spec, _pid = fixture(tmp_path)
    select(spec)
    manifest = recovery_bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    result = recovery_bundle.restore(tmp_path / "backup", tmp_path / "restored",
                                    manifest_sha256=manifest["manifestSha256"])
    assert result["institutionalRecovery"]["programs"]["verifiedAccountingBindings"] == 1
    assert result["institutionalRecovery"]["programs"]["legacyMissingAccountingBindings"] == 0
    assert result["institutionalRecovery"]["activationAuthorized"] is False


def test_legacy_unbound_rows_are_not_assigned_a_new_historical_selection(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    try:
        create = h.programs.create
        def legacy_create(**kwargs):
            kwargs.pop("accounting_scope_payload", None)
            return create(**kwargs)
        monkeypatch.setattr(h.programs, "create", legacy_create)
        prepared = h.coordinator.prepare(make_request(h.broker))
        assert prepared.program.accounting_scope_payload is None
        assert h.programs.accounting_scopes("tenant") == ()
    finally:
        h.close()


def test_two_connections_prepare_one_same_selection(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    h, other = Harness(tmp_path), Harness(tmp_path)
    try:
        request = make_request(h.broker)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda c: c.prepare(request), (h.coordinator, other.coordinator)))
        assert all(r.approved for r in results)
        assert results[0].program.accounting_scope_payload == results[1].program.accounting_scope_payload
        assert h.programs.db.execute("SELECT count(*) FROM execution_programs").fetchone()[0] == 1
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        other.close(); h.close()


def test_independent_protection_still_closes_after_accounting_metadata_is_invalid(tmp_path):
    from quant_ai.execution.protective_exits import ProtectiveExitEngine
    h = Harness(tmp_path)
    try:
        prepared = h.coordinator.prepare(make_request(h.broker))
        assert h.coordinator.execute_due(prepared.program.program_id, now=NOW).stage.value == "COMPLETE"
        h.journal.db.execute("DROP TRIGGER trading_journal_store_identity_immutable")
        h.journal.db.execute("UPDATE trading_journal_meta SET store_identity=NULL")
        h.journal.db.commit()
        exits = ProtectiveExitEngine(h.broker, lambda _: Decimal(90), tenant_id="tenant").evaluate()
        assert exits and exits[0].filled
        assert h.broker.get_positions("tenant") == ()
        assert h.coordinator.reconcile_protective_accounting(currency="INR").status == "unavailable"
    finally:
        h.close()


def test_competing_first_selections_cannot_split_a_tenant_across_two_stores(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from quant_ai.execution.program import ExecutionProgramJournal
    h = Harness(tmp_path)
    other = replacement(tmp_path)
    programs = ExecutionProgramJournal(tmp_path / "programs.sqlite")
    try:
        coordinator = restarted(h, other, programs)
        barrier = Barrier(2)
        def prepare(pair):
            runtime, decision = pair
            request = make_request(h.broker, p=proposal(decision_id=decision))
            barrier.wait(timeout=10)
            return runtime.prepare(request)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(prepare, ((h.coordinator, "first"), (coordinator, "second"))))
        assert sum(r.approved for r in results) == 1
        assert [r.reason for r in results if not r.approved] == ["institutional_accounting_binding_mismatch"]
        assert h.programs.db.execute("SELECT count(*) FROM execution_programs").fetchone()[0] == 1
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        programs.close(); other.close(); h.close()


def test_failed_parent_transaction_keeps_only_unused_local_identity(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    try:
        create = h.programs.create
        def interrupted(**kwargs):
            create(**kwargs)
            raise RuntimeError("synthetic parent transaction failure")
        monkeypatch.setattr(h.programs, "create", interrupted)
        with pytest.raises(RuntimeError, match="synthetic parent"):
            h.coordinator.prepare(make_request(h.broker))
        assert h.programs.db.execute("SELECT count(*) FROM execution_programs").fetchone()[0] == 0
        assert h.programs.accounting_scopes("tenant") == ()
        assert h.broker.ledger_entries("tenant") == ()
        assert h.journal.db.execute("SELECT count(*) FROM trading_transactions WHERE kind!='CAPITAL'").fetchone()[0] == 0
        monkeypatch.setattr(h.programs, "create", create)
        assert h.coordinator.prepare(make_request(h.broker)).approved
    finally:
        h.close()


@pytest.mark.parametrize("boundary", ["before_parent_commit", "after_parent_commit"])
def test_subprocess_death_preserves_identity_and_exact_parent_commit(tmp_path, boundary):
    import os
    import subprocess
    import sys
    from pathlib import Path
    code = r"""
import os, sys
from pathlib import Path
from test_institutional_paper_coordinator import Harness, make_request
root, boundary = Path(sys.argv[1]), sys.argv[2]
h = Harness(root)
if boundary == 'before_parent_commit':
    create = h.programs.create
    def interrupted(**kwargs):
        create(**kwargs)
        os._exit(81)
    h.programs.create = interrupted
result = h.coordinator.prepare(make_request(h.broker))
assert result.approved
os._exit(82)
"""
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(root / "src"), str(root / "tests"))),
           "TRADING_LIVE_MONEY_ACTIVE": "false"}
    completed = subprocess.run([sys.executable, "-c", code, str(tmp_path), boundary],
        env=env, cwd=root, capture_output=True, text=True, timeout=30, check=False)
    assert completed.returncode == (81 if boundary == "before_parent_commit" else 82), completed.stderr
    h = Harness(tmp_path)
    try:
        rows = h.programs.db.execute("SELECT program_id,accounting_scope_payload FROM execution_programs").fetchall()
        assert len(rows) == (0 if boundary == "before_parent_commit" else 1)
        assert h.journal.db.execute("SELECT store_identity FROM trading_journal_meta").fetchone()[0]
        if rows:
            assert rows[0][1]
            h.coordinator.restore_runtime_context(rows[0][0], tenant_id="tenant")
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        h.close()
