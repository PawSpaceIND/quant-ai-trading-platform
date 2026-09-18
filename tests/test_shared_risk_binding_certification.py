"""Independent paper binding checks; never release reserved capacity or call providers."""
import json
import sqlite3
from pathlib import Path

import pytest
from test_institutional_paper_coordinator import NOW, Harness, make_request
from test_shared_risk_reservations import enable, policy


def _change_receipt(db, value):
    row = db.execute("SELECT order_id,payload FROM paper_decision_evidence").fetchone()
    payload = json.loads(row[1])
    if value is None:
        payload.pop("shared_risk_binding_sha256", None)
    else:
        payload["shared_risk_binding_sha256"] = value
    # A deliberately corrupted disposable fixture, not a runtime write path.
    for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='paper_decision_evidence'").fetchall():
        assert name.replace("_", "").isalnum()
        db.execute(f'DROP TRIGGER "{name}"')
    db.execute("UPDATE paper_decision_evidence SET payload=? WHERE order_id=?",
               (json.dumps(payload), row[0]))
    db.commit()


@pytest.mark.parametrize("value", [None, "f" * 64, True])
def test_receipt_read_rejects_missing_or_changed_broker_binding(tmp_path, value):
    h = Harness(tmp_path)
    try:
        enable(h)
        prepared = h.coordinator.prepare(make_request(h.broker))
        assert h.coordinator.execute_due(prepared.program.program_id, now=NOW).stage.value == "COMPLETE"
        _change_receipt(h.broker._connection, value)
        before = tuple(h.broker._connection.iterdump())
        with pytest.raises(ValueError, match="shared_risk_broker_receipt_binding"):
            h.broker.submission_receipt(f"{prepared.program.program_id}:1", "tenant")
        assert tuple(h.broker._connection.iterdump()) == before
    finally:
        h.close()


@pytest.mark.parametrize("value", [None, "f" * 64, True])
def test_offline_backup_rejects_receipt_with_wrong_broker_binding(tmp_path, monkeypatch, value):
    from test_institutional_state_bundle import fixture, select

    from quant_ai.execution.institutional import InstitutionalPaperCoordinator
    from quant_ai.operations import recovery_bundle
    original = InstitutionalPaperCoordinator.__init__
    def selected(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.shared_risk_policy = policy()
    monkeypatch.setattr(InstitutionalPaperCoordinator, "__init__", selected)
    spec, _ = fixture(tmp_path)
    select(spec)
    path = Path(spec["sources"]["ledger"])
    with sqlite3.connect(path) as db:
        _change_receipt(db, value)
    db.close()
    with pytest.raises(ValueError, match="shared_risk_broker_receipt_binding"):
        recovery_bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    assert not (tmp_path / "backup").exists()


@pytest.mark.parametrize("phase", ["before_pin", "after_pin", "after_commit"])
def test_abrupt_bootstrap_never_reopens_bound_account_as_unrestricted(tmp_path, phase, monkeypatch):
    import os
    import subprocess
    import sys

    from test_shared_risk_account_binding import _direct_order

    from quant_ai.execution.paper_ledger import PaperBrokerService
    from quant_ai.execution.program import ExecutionProgramJournal
    from quant_ai.execution.shared_risk_binding import read_binding, verify_binding_pair
    monkeypatch.delenv("PYTHONPATH", raising=False)
    root = Path(__file__).resolve().parents[1]
    code = """
import os,sys
from pathlib import Path
sys.path[:0]=[str(Path(sys.argv[1])/'src'),str(Path(sys.argv[1])/'tests')]
from test_institutional_paper_coordinator import Harness,make_request
from test_shared_risk_reservations import enable
from quant_ai.execution import institutional
h=Harness(Path(sys.argv[2]));enable(h)
phase=sys.argv[3]
original=institutional.pin_account
if phase!='after_commit':
 def terminate(*args,**kwargs):
  if phase=='after_pin':original(*args,**kwargs)
  os._exit(73)
 institutional.pin_account=terminate
assert h.coordinator.prepare(make_request(h.broker)).approved
os._exit(73)
"""
    result = subprocess.run([sys.executable, "-c", code, str(root), str(tmp_path), phase],
        env={**os.environ, "TRADING_LIVE_MONEY_ACTIVE":"false"},capture_output=True,
        timeout=30,check=False)
    assert result.returncode == 73, result.stderr.decode()
    broker=PaperBrokerService(tmp_path/"paper.sqlite")
    programs=ExecutionProgramJournal(tmp_path/"programs.sqlite")
    try:
        count=programs.db.execute("SELECT COUNT(*) FROM execution_programs").fetchone()[0]
        pin=read_binding(broker._connection,"tenant")
        assert count == (1 if phase=="after_commit" else 0)
        if phase=="before_pin":
            assert pin is None
        else:
            assert pin is not None
            with pytest.raises(ValueError,match="shared_risk_broker"):
                broker.buy(_direct_order())
            if phase=="after_pin":
                with pytest.raises(ValueError,match="journal_binding_missing"):
                    verify_binding_pair(broker._connection,programs.db,"tenant")
            else:
                assert verify_binding_pair(broker._connection,programs.db,"tenant")==pin
        assert broker.ledger_entries("tenant")==()
    finally:
        programs.close();broker.close()


def test_independent_protective_exit_survives_unavailable_selected_journal(tmp_path):
    from decimal import Decimal

    from quant_ai.execution.protective_exits import ProtectiveExitEngine
    h=Harness(tmp_path)
    try:
        enable(h);prepared=h.coordinator.prepare(make_request(h.broker))
        assert h.coordinator.execute_due(prepared.program.program_id,now=NOW).stage.value=="COMPLETE"
        h.programs.close();h.programs.path.rename(tmp_path/"removed-selected-journal.sqlite")
        outcomes=ProtectiveExitEngine(h.broker,lambda _:Decimal(90),tenant_id="tenant").evaluate()
        assert len(outcomes)==1 and outcomes[0].filled
        assert h.broker.get_positions("tenant")==()
        assert len(h.broker.ledger_entries("tenant"))==2
    finally:
        h.close()


def test_competing_journals_cannot_both_pin_the_same_empty_paper_account(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from quant_ai.execution.program import ExecutionProgramJournal
    from quant_ai.execution.shared_risk import SharedRiskReservations
    h1,h2=Harness(tmp_path),Harness(tmp_path)
    alternate=ExecutionProgramJournal(tmp_path/"different-journal.sqlite")
    try:
        enable(h1);enable(h2)
        h2.coordinator.programs=alternate
        h2.coordinator.shared_risk=SharedRiskReservations(alternate)
        barrier=Barrier(2)
        def prepare(h):
            request=make_request(h.broker)
            barrier.wait(timeout=5)
            return h.coordinator.prepare(request)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(prepare,(h1,h2)))
        assert sum(r.approved for r in results)==1
        assert all("shared_risk_broker" in r.reason for r in results if not r.approved)
        assert h1.programs.db.execute("SELECT COUNT(*) FROM execution_programs").fetchone()[0]+alternate.db.execute("SELECT COUNT(*) FROM execution_programs").fetchone()[0]==1
        assert h1.broker.ledger_entries("tenant")==()
    finally:
        alternate.close();h2.close();h1.close()


def test_final_broker_checks_open_selected_journal_read_only(tmp_path,monkeypatch):
    from quant_ai.execution import shared_risk_binding
    h=Harness(tmp_path)
    try:
        enable(h);prepared=h.coordinator.prepare(make_request(h.broker))
        calls=[]
        original=sqlite3.connect
        def record(path,*args,**kwargs):
            calls.append((path,kwargs))
            return original(path,*args,**kwargs)
        monkeypatch.setattr(shared_risk_binding.sqlite3,"connect",record)
        assert h.coordinator.execute_due(prepared.program.program_id,now=NOW).stage.value=="COMPLETE"
        assert calls and all(path.endswith("?mode=ro") and kw.get("uri") is True for path,kw in calls)
    finally:
        h.close()


@pytest.mark.parametrize("version", [0, 2])
def test_final_broker_rejects_inconsistent_authority_version(tmp_path, version):
    from test_shared_risk_account_binding import _claimed
    h=Harness(tmp_path)
    try:
        _prepared,child,evidence,key=_claimed(h)
        for (name,) in h.programs.db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='execution_programs'").fetchall():
            assert name.replace("_", "").isalnum()
            h.programs.db.execute(f'DROP TRIGGER "{name}"')
        h.programs.db.execute("UPDATE execution_programs SET risk_authority_version=?",(version,))
        h.programs.db.commit()
        with pytest.raises(ValueError,match="shared_risk_broker_program_authority_invalid"):
            h.broker.submit_with_evidence(child,evidence,key)
        assert h.broker.ledger_entries("tenant")==()
    finally:
        h.close()


@pytest.mark.parametrize("rejected_setup", [False, True])
def test_unselected_or_refused_account_keeps_replay_table_inventory(tmp_path,rejected_setup):
    from dataclasses import replace
    from decimal import Decimal

    from quant_ai.execution.shared_risk_binding import TABLE
    h=Harness(tmp_path)
    try:
        if rejected_setup:
            enable(h)
            h.coordinator.shared_risk_policy=replace(policy(),max_loss_fraction=Decimal(".0001"))
            result=h.coordinator.prepare(make_request(h.broker))
            assert not result.approved and result.reason=="shared_risk_budget_exceeded"
        assert h.broker._connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(TABLE,)).fetchone() is None
        assert h.broker.ledger_entries("tenant")==()
    finally:
        h.close()
