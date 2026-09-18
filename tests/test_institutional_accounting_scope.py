"""Synthetic coordinator/accounting tenant isolation; never call a live broker."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from test_institutional_paper_coordinator import NOW, Harness, make_request

from quant_ai.accounting.journal import TradingJournal
from quant_ai.accounting.trading import TradingAccounting
from quant_ai.domain.models import Side
from quant_ai.execution.institutional import InstitutionalPaperCoordinator
from quant_ai.execution.planner import ExecutionAlgorithm, VolumeBucket
from quant_ai.execution.program import SliceState


def test_preparation_refuses_wrong_accounting_tenant_before_creating_program(tmp_path):
    h = Harness(tmp_path)
    try:
        h.coordinator.accounting = TradingAccounting(h.journal, "other")
        before = tuple(h.journal.db.iterdump())
        result = h.coordinator.prepare(make_request(h.broker))
        assert not result.approved
        assert result.reason == "institutional_accounting_scope_mismatch"
        assert h.programs.db.execute("SELECT count(*) FROM execution_programs").fetchone()[0] == 0
        assert tuple(h.journal.db.iterdump()) == before
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        h.close()


def test_saved_context_cannot_rebind_to_a_different_accounting_tenant(tmp_path):
    h = Harness(tmp_path)
    try:
        result = h.coordinator.prepare(make_request(h.broker))
        h.coordinator._requests.clear(); h.coordinator._orders.clear()
        h.coordinator.accounting = TradingAccounting(h.journal, "other")
        with pytest.raises(ValueError, match="institutional_accounting_scope_mismatch"):
            h.coordinator.restore_runtime_context(result.program.program_id, tenant_id="tenant")
        assert not h.coordinator._requests and not h.coordinator._orders
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        h.close()


def test_tenant_drift_after_broker_commit_never_posts_to_the_other_tenant(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    try:
        result = h.coordinator.prepare(make_request(h.broker))
        submit = h.broker.submit_with_evidence
        def committed_then_changed(*args, **kwargs):
            fill = submit(*args, **kwargs)
            h.accounting.tenant_id = "other"
            return fill
        monkeypatch.setattr(h.broker, "submit_with_evidence", committed_then_changed)
        done = h.coordinator.execute_due(result.program.program_id, now=NOW)
        assert done.reason == "accounting_reconciliation_required:institutional_accounting_scope_mismatch"
        assert done.program.slices[0].state is SliceState.FILLED_UNACCOUNTED
        assert len(h.broker.ledger_entries("tenant")) == 1
        assert h.journal.db.execute("SELECT count(*) FROM trading_transactions WHERE tenant_id='other'").fetchone()[0] == 0
        assert h.journal.native_balance("tenant", "SECURITIES_COST", "INR") == Decimal(0)
    finally:
        h.close()


D = Decimal


def clone(h, **overrides):
    values = {name: getattr(h.coordinator, name) for name in (
        "broker", "oms", "programs", "accounting", "warden", "snapshot_provider",
        "factor_position_provider", "strategy_exposure_provider", "slice_volume_provider",
        "edge_gate", "optimizer", "execution_planner", "shared_risk_policy")}
    return InstitutionalPaperCoordinator(**{**values, **overrides})


def no_foreign_postings(h):
    assert h.journal.db.execute(
        "SELECT count(*) FROM trading_transactions WHERE tenant_id != 'tenant'").fetchone()[0] == 0


@pytest.mark.parametrize("tenant", [None, "", " ", "tenant ", 1, True])
def test_coordinator_requires_explicit_well_formed_accounting_namespace(tmp_path, tenant):
    h = Harness(tmp_path)
    try:
        with pytest.raises(ValueError, match="institutional_accounting_scope_invalid"):
            clone(h, accounting=TradingAccounting(h.journal, tenant))
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        h.close()


@pytest.mark.parametrize("entry_point", ["prepare", "bind", "restore", "execute", "recover"])
def test_accounting_namespace_change_blocks_every_coordinator_entry_point(tmp_path, entry_point):
    h = Harness(tmp_path)
    try:
        request = make_request(h.broker)
        prepared = h.coordinator.prepare(request)
        pid = prepared.program.program_id
        before_programs = tuple(h.programs.db.iterdump())
        before_accounting = tuple(h.journal.db.iterdump())
        h.accounting.tenant_id = "other"
        if entry_point == "prepare":
            result = h.coordinator.prepare(request)
            assert not result.approved and result.reason == "institutional_accounting_scope_mismatch"
        else:
            with pytest.raises(ValueError, match="institutional_accounting_scope_mismatch"):
                if entry_point == "bind":
                    h.coordinator.bind_runtime_context(pid, request=request, parent_order=prepared.approved_order)
                elif entry_point == "restore":
                    h.coordinator.restore_runtime_context(pid, tenant_id="tenant")
                elif entry_point == "execute":
                    h.coordinator.execute_due(pid, now=NOW)
                else:
                    h.coordinator.reconcile_accounting(pid)
        assert tuple(h.programs.db.iterdump()) == before_programs
        assert tuple(h.journal.db.iterdump()) == before_accounting
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        h.close()


@pytest.mark.parametrize("field", ["adapter", "journal", "connection", "currency"])
def test_same_tenant_label_cannot_redirect_an_existing_coordinator_to_another_store(tmp_path, field):
    h = Harness(tmp_path)
    other = TradingJournal(tmp_path / "other.sqlite", base_currency="INR")
    original_db, original_currency = h.journal.db, h.journal.base_currency
    try:
        request = make_request(h.broker)
        prepared = h.coordinator.prepare(request)
        foreign_before = tuple(other.db.iterdump())
        if field == "adapter":
            h.coordinator.accounting = TradingAccounting(other, "tenant")
        elif field == "journal":
            h.accounting.journal = other
        elif field == "connection":
            h.journal.db = other.db
        else:
            h.journal.base_currency = "USD"
        with pytest.raises(ValueError, match="institutional_accounting_scope_mismatch"):
            h.coordinator.execute_due(prepared.program.program_id, now=NOW)
        assert tuple(other.db.iterdump()) == foreign_before
        assert h.programs.get(prepared.program.program_id).slices[0].state is SliceState.PENDING
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        h.accounting.journal = h.journal
        h.journal.db, h.journal.base_currency = original_db, original_currency
        other.close(); h.close()


@pytest.mark.parametrize("callback", ["strategy", "planner", "warden"])
def test_scope_change_inside_preparation_callback_never_creates_a_program(tmp_path, monkeypatch, callback):
    h = Harness(tmp_path)
    try:
        if callback == "strategy":
            target, name = h.coordinator, "strategy_exposure_provider"
        elif callback == "planner":
            target, name = h.coordinator.execution_planner, "plan"
        else:
            target, name = h.coordinator.warden, "evaluate"
        real = getattr(target, name)
        def changed(*args, **kwargs):
            result = real(*args, **kwargs)
            h.accounting.tenant_id = "other"
            return result
        monkeypatch.setattr(target, name, changed)
        result = h.coordinator.prepare(make_request(h.broker))
        assert not result.approved and result.reason == "institutional_accounting_scope_mismatch"
        assert h.programs.db.execute("SELECT count(*) FROM execution_programs").fetchone()[0] == 0
        assert h.broker.ledger_entries("tenant") == ()
        no_foreign_postings(h)
    finally:
        h.close()


@pytest.mark.parametrize("callback", ["snapshot", "strategy", "factor", "volume", "warden", "oms"])
def test_scope_drift_inside_dispatch_callbacks_never_reaches_the_broker(tmp_path, monkeypatch, callback):
    h = Harness(tmp_path)
    try:
        request = make_request(h.broker)
        prepared = h.coordinator.prepare(request)
        targets = {"snapshot": (h.coordinator, "snapshot_provider"),
            "strategy": (h.coordinator, "strategy_exposure_provider"),
            "factor": (h.coordinator, "factor_position_provider"),
            "volume": (h.coordinator, "slice_volume_provider"),
            "warden": (h.coordinator.warden, "evaluate"), "oms": (h.oms, "submitted")}
        target, name = targets[callback]
        real = getattr(target, name)
        def changed(*args, **kwargs):
            value = real(*args, **kwargs)
            h.accounting.tenant_id = "other"
            return value
        monkeypatch.setattr(target, name, changed)
        def forbidden(*args, **kwargs):
            pytest.fail("A drifted accounting selection must not submit another paper order")
        monkeypatch.setattr(h.broker, "submit_with_evidence", forbidden)
        if callback == "oms":
            result = h.coordinator.execute_due(prepared.program.program_id, now=NOW)
            assert result.stage.value == "RECOVERY_REQUIRED"
            assert "institutional_accounting_scope_mismatch" in result.reason
        else:
            with pytest.raises(ValueError, match="institutional_accounting_scope_mismatch"):
                h.coordinator.execute_due(prepared.program.program_id, now=NOW)
        assert h.programs.get(prepared.program.program_id).slices[0].state is SliceState.DISPATCHING
        assert h.broker.ledger_entries("tenant") == ()
        no_foreign_postings(h)
    finally:
        h.close()


@pytest.mark.parametrize("when", ["before_post", "after_post", "exception_after_post"])
def test_mid_posting_scope_drift_or_exception_rolls_back_the_whole_journal_transaction(tmp_path, monkeypatch, when):
    h = Harness(tmp_path)
    try:
        prepared = h.coordinator.prepare(make_request(h.broker))
        original = h.accounting.buy_security
        before = tuple(h.journal.db.iterdump())
        def changed(*args, **kwargs):
            if when == "before_post":
                h.accounting.tenant_id = "other"
            value = original(*args, **kwargs)
            if when == "exception_after_post":
                raise ValueError("synthetic interruption after posting")
            h.accounting.tenant_id = "other"
            return value
        monkeypatch.setattr(h.accounting, "buy_security", changed)
        result = h.coordinator.execute_due(prepared.program.program_id, now=NOW)
        assert result.reason.startswith("accounting_reconciliation_required:")
        assert result.program.slices[0].state is SliceState.FILLED_UNACCOUNTED
        assert tuple(h.journal.db.iterdump()) == before
        assert len(h.broker.ledger_entries("tenant")) == 1
        h.accounting.tenant_id = "tenant"
        monkeypatch.setattr(h.accounting, "buy_security", original)
        coordinator = clone(h)
        coordinator.restore_runtime_context(prepared.program.program_id, tenant_id="tenant")
        assert coordinator.reconcile_accounting(prepared.program.program_id).stage.value == "COMPLETE"
        assert coordinator.reconcile_accounting(prepared.program.program_id).executed_sequences == ()
        assert len(h.broker.ledger_entries("tenant")) == 1
        assert h.journal.native_balance("tenant", "SECURITIES_COST", "INR") == D(1000)
        no_foreign_postings(h)
    finally:
        h.close()


def test_completed_first_slice_survives_scope_hold_and_explicit_restart(tmp_path):
    from test_institutional_paper_coordinator import proposal
    h = Harness(tmp_path)
    try:
        request = make_request(h.broker, p=proposal(quantity=20), algorithm=ExecutionAlgorithm.TWAP,
            buckets=(VolumeBucket(NOW,1000),VolumeBucket(NOW+timedelta(minutes=10),1000)))
        prepared = h.coordinator.prepare(request)
        assert h.coordinator.execute_due(prepared.program.program_id, now=NOW).executed_sequences == (1,)
        h.accounting.tenant_id = "other"
        with pytest.raises(ValueError, match="institutional_accounting_scope_mismatch"):
            h.coordinator.execute_due(prepared.program.program_id, now=NOW+timedelta(minutes=10))
        assert len(h.broker.ledger_entries("tenant")) == 1
        h.accounting.tenant_id = "tenant"
        coordinator = clone(h)
        coordinator.restore_runtime_context(prepared.program.program_id, tenant_id="tenant")
        result = coordinator.execute_due(prepared.program.program_id, now=NOW+timedelta(minutes=10))
        assert result.stage.value == "COMPLETE" and result.executed_sequences == (2,)
        assert len(h.broker.ledger_entries("tenant")) == 2
        no_foreign_postings(h)
    finally:
        h.close()


def test_independent_exit_is_not_blocked_by_a_broken_coordinator_accounting_selection(tmp_path):
    from quant_ai.execution.protective_exits import ProtectiveExitEngine
    h = Harness(tmp_path)
    try:
        prepared = h.coordinator.prepare(make_request(h.broker))
        assert h.coordinator.execute_due(prepared.program.program_id, now=NOW).stage.value == "COMPLETE"
        before = tuple(h.journal.db.iterdump())
        h.accounting.tenant_id = "other"
        report = h.coordinator.reconcile_protective_accounting(currency="INR")
        assert report.status == "unavailable" and report.reason == "institutional_accounting_scope_mismatch"
        assert tuple(h.journal.db.iterdump()) == before
        assert ProtectiveExitEngine(h.broker,lambda _:D(90),tenant_id="tenant").evaluate()[0].filled
        assert h.broker.get_positions("tenant") == ()
        h.accounting.tenant_id = "tenant"
        assert h.coordinator.reconcile_protective_accounting(currency="INR").status == "matched"
        assert h.journal.native_balance("tenant", "SECURITIES_COST", "INR") == D(0)
        no_foreign_postings(h)
    finally:
        h.close()


def test_correctly_scoped_covered_sell_still_reconciles_without_entry_evidence(tmp_path):
    from test_institutional_paper_coordinator import proposal
    h = Harness(tmp_path)
    try:
        entry = h.coordinator.prepare(make_request(h.broker))
        h.coordinator.execute_due(entry.program.program_id,now=NOW)
        request = replace(make_request(h.broker,p=proposal(side=Side.SELL,decision_id="exit")), edge_evidence=None)
        sale = h.coordinator.prepare(request)
        assert sale.approved
        assert h.coordinator.execute_due(sale.program.program_id,now=NOW).stage.value == "COMPLETE"
        assert h.broker.get_positions("tenant") == ()
        assert h.journal.native_balance("tenant", "SECURITIES_COST", "INR") == D(0)
        no_foreign_postings(h)
    finally:
        h.close()


@pytest.mark.parametrize("boundary,code", [("uncommitted_foreign_post", 91), ("committed_correct_post", 92)])
def test_process_death_does_not_commit_a_wrong_tenant_or_duplicate_the_fill(tmp_path, boundary, code):
    import os
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    script = """import os,sys
from pathlib import Path
from test_institutional_paper_coordinator import Harness,NOW,make_request
h=Harness(Path(sys.argv[1]))
p=h.coordinator.prepare(make_request(h.broker))
if sys.argv[2]=='uncommitted_foreign_post':
    original=h.accounting.buy_security
    def interrupted(*args,**kwargs):
        h.accounting.tenant_id='other'
        original(*args,**kwargs)
        os._exit(91)
    h.accounting.buy_security=interrupted
else:
    def interrupted(*args,**kwargs):os._exit(92)
    h.programs.mark_executed=interrupted
h.coordinator.execute_due(p.program.program_id,now=NOW)
raise RuntimeError('Expected interruption did not happen')
"""
    env={**os.environ, "PYTHONPATH": os.pathsep.join([str(root/'src'),str(root/'tests')]),
         "TRADING_LIVE_MONEY_ACTIVE": "false"}
    result=subprocess.run([sys.executable,'-c',script,str(tmp_path),boundary],cwd=root,env=env,
                          text=True,capture_output=True,timeout=30,check=False)
    assert result.returncode==code,result.stdout+result.stderr
    h=Harness(tmp_path)
    try:
        pid=h.programs.db.execute("SELECT program_id FROM execution_programs").fetchone()[0]
        assert len(h.broker.ledger_entries('tenant'))==1
        no_foreign_postings(h)
        assert h.programs.get(pid).slices[0].state is SliceState.FILLED_UNACCOUNTED
        expected=D(0) if boundary=='uncommitted_foreign_post' else D(1000)
        assert h.journal.native_balance('tenant','SECURITIES_COST','INR')==expected
        h.coordinator.restore_runtime_context(pid,tenant_id='tenant')
        assert h.coordinator.reconcile_accounting(pid).stage.value=='COMPLETE'
        assert h.coordinator.reconcile_accounting(pid).executed_sequences==()
        assert len(h.broker.ledger_entries('tenant'))==1
        assert h.journal.native_balance('tenant','SECURITIES_COST','INR')==D(1000)
        no_foreign_postings(h)
    finally:h.close()
