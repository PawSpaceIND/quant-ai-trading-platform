"""Local paper-account journal identity; no network or risk-release operations."""
import sqlite3
from dataclasses import replace
from decimal import Decimal

import pytest
from test_institutional_paper_coordinator import NOW, Harness, make_request, proposal
from test_shared_risk_reservations import enable

from quant_ai.domain.models import Market, OrderIntent, Side
from quant_ai.execution.program import ExecutionProgramJournal
from quant_ai.execution.shared_risk import SharedRiskReservations

D = Decimal


def test_reserved_account_refuses_unlinked_buy(tmp_path):
    h = Harness(tmp_path)
    try:
        enable(h)
        assert h.coordinator.prepare(make_request(h.broker)).approved
        before = h.broker.get_margin("tenant").cash_balance
        with pytest.raises(ValueError, match="shared_risk_broker"):
            h.broker.buy(OrderIntent("INFY", Market.INDIA, Side.BUY, 10, D(100),
                "unlinked", tenant_id="tenant", stop_price=D(95)))
        assert h.broker.get_margin("tenant").cash_balance == before
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        h.close()


def test_another_program_journal_cannot_discard_existing_reservations(tmp_path):
    h = Harness(tmp_path)
    alternative = ExecutionProgramJournal(tmp_path / "other-programs.sqlite")
    try:
        enable(h)
        assert h.coordinator.prepare(make_request(h.broker)).approved
        h.coordinator.programs = alternative
        h.coordinator.shared_risk = SharedRiskReservations(alternative)
        candidate = h.coordinator.prepare(make_request(h.broker, p=proposal(decision_id="other")))
        assert not candidate.approved
        assert "shared_risk_broker" in candidate.reason
        assert alternative.db.execute("SELECT COUNT(*) FROM execution_programs").fetchone()[0] == 0
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        alternative.close()
        h.close()


def test_another_journal_cannot_disable_shared_mode(tmp_path):
    h = Harness(tmp_path)
    alternative = ExecutionProgramJournal(tmp_path / "legacy-programs.sqlite")
    try:
        enable(h)
        assert h.coordinator.prepare(make_request(h.broker)).approved
        h.coordinator.programs = alternative
        h.coordinator.shared_risk = SharedRiskReservations(alternative)
        h.coordinator.shared_risk_policy = None
        candidate = h.coordinator.prepare(make_request(h.broker, p=proposal(decision_id="legacy")))
        assert not candidate.approved and "shared_risk_broker" in candidate.reason
        assert alternative.db.execute("SELECT COUNT(*) FROM execution_programs").fetchone()[0] == 0
    finally:
        alternative.close()
        h.close()


def _direct_order(tenant="tenant"):
    return OrderIntent("INFY", Market.INDIA, Side.BUY, 10, D(100),
        "unlinked", tenant_id=tenant, stop_price=D(95))


def _claimed(h, *, claim=True):
    enable(h)
    prepared = h.coordinator.prepare(make_request(h.broker))
    assert prepared.approved
    pid = prepared.program.program_id
    child = prepared.approved_order
    from quant_ai.orders.oms import DurableOms
    cid = DurableOms.client_order_id(child, "decision-1:slice:1")
    if claim:
        assert h.programs.claim_slice(pid, 1, client_order_id=cid)
    evidence = {"schema":"pramana.swarm_fill.v1", "event_type":"swarm_fill",
        "institutional_program":pid, "institutional_slice":1,
        "institutional_risk_authority_sha256":prepared.program.runtime_context_sha256}
    return prepared, child, evidence, f"{pid}:1"


def test_normal_governed_fill_retains_broker_owned_binding(tmp_path):
    from quant_ai.execution.shared_risk_binding import read_binding
    h=Harness(tmp_path)
    try:
        enable(h); prepared=h.coordinator.prepare(make_request(h.broker))
        pin=read_binding(h.broker._connection,"tenant")
        assert pin["journalId"] == h.programs.db.execute("SELECT journal_id FROM shared_risk_accounts").fetchone()[0]
        assert h.coordinator.execute_due(prepared.program.program_id,now=NOW).stage.value == "COMPLETE"
        evidence=h.broker.submission_receipt(f"{prepared.program.program_id}:1","tenant").evidence
        witness=h.broker._connection.execute("SELECT shared_risk_binding_sha256 FROM paper_accounts WHERE tenant_id='tenant'").fetchone()[0]
        assert evidence["shared_risk_binding_sha256"]==witness
        assert len(h.broker.ledger_entries("tenant"))==1
        assert h.journal.native_balance("tenant","SECURITIES_COST","INR")==D(1000)
    finally:h.close()


def test_local_binding_survives_broker_restart(tmp_path):
    from quant_ai.execution.paper_ledger import PaperBrokerService
    h=Harness(tmp_path)
    try:
        enable(h);assert h.coordinator.prepare(make_request(h.broker)).approved
        second=PaperBrokerService(tmp_path/"paper.sqlite",slippage_bps=D(0))
        try:
            with pytest.raises(ValueError,match="shared_risk_broker_reserved_child_required"):
                second.buy(_direct_order())
            assert second.ledger_entries("tenant")==()
        finally:second.close()
    finally:h.close()


def test_same_path_empty_journal_replacement_cannot_bootstrap_again(tmp_path):
    h=Harness(tmp_path)
    replacement=None
    try:
        enable(h);assert h.coordinator.prepare(make_request(h.broker)).approved
        path=h.programs.path
        h.programs.close()
        path.rename(tmp_path/"saved-programs.sqlite")
        replacement=ExecutionProgramJournal(path)
        h.coordinator.programs=replacement
        h.coordinator.shared_risk=SharedRiskReservations(replacement)
        result=h.coordinator.prepare(make_request(h.broker,p=proposal(decision_id="replaced")))
        assert not result.approved and result.reason=="shared_risk_broker_journal_binding_missing"
        assert replacement.db.execute("SELECT COUNT(*) FROM execution_programs").fetchone()[0]==0
        assert h.broker.ledger_entries("tenant")==()
    finally:
        if replacement:replacement.close()
        h.close()


def test_copied_journal_at_other_path_is_not_the_selected_journal(tmp_path):
    h=Harness(tmp_path)
    copied=None
    try:
        enable(h);assert h.coordinator.prepare(make_request(h.broker)).approved
        with sqlite3.connect(tmp_path/"copied.sqlite") as out:
            h.programs.db.backup(out)
        out.close()
        copied=ExecutionProgramJournal(tmp_path/"copied.sqlite")
        h.coordinator.programs=copied;h.coordinator.shared_risk=SharedRiskReservations(copied)
        result=h.coordinator.prepare(make_request(h.broker,p=proposal(decision_id="copied")))
        assert not result.approved and result.reason=="shared_risk_broker_journal_path_mismatch"
        assert h.broker.ledger_entries("tenant")==()
    finally:
        if copied:copied.close()
        h.close()


@pytest.mark.parametrize("kind",["account_row","account_table","reservation_row","witness","pin_row","pin_table","journal_id"])
def test_missing_or_rewritten_binding_fails_without_a_paper_fill(tmp_path,kind):
    h=Harness(tmp_path)
    try:
        _prepared,child,evidence,key=_claimed(h)
        if kind=="account_row":
            h.programs.db.execute("DROP TRIGGER shared_risk_accounts_delete_blocked")
            h.programs.db.execute("DELETE FROM shared_risk_accounts")
        elif kind=="account_table":
            h.programs.db.execute("DROP TABLE shared_risk_accounts")
        elif kind=="reservation_row":
            h.programs.db.execute("DROP TRIGGER shared_risk_reservations_delete_blocked")
            h.programs.db.execute("DELETE FROM shared_risk_reservations")
        elif kind=="journal_id":
            h.programs.db.execute("DROP TRIGGER shared_risk_accounts_update_blocked")
            h.programs.db.execute("UPDATE shared_risk_accounts SET journal_id=?",("a"*32,))
        elif kind=="pin_row":
            h.broker._connection.execute("DROP TRIGGER shared_broker_binding_delete_blocked")
            h.broker._connection.execute("DELETE FROM paper_shared_risk_bindings")
        elif kind=="pin_table":
            h.broker._connection.execute("DROP TABLE paper_shared_risk_bindings")
        else:
            h.broker._connection.execute("DROP TRIGGER shared_broker_witness_update_blocked")
            h.broker._connection.execute("UPDATE paper_accounts SET shared_risk_binding_sha256=?",("b"*64,))
        h.programs.db.commit();h.broker._connection.commit()
        before=tuple(h.broker._connection.iterdump())
        with pytest.raises(ValueError,match="shared_risk_broker"):
            h.broker.submit_with_evidence(child,evidence,key)
        assert tuple(h.broker._connection.iterdump())==before
        assert h.broker.ledger_entries("tenant")==()
    finally:h.close()


@pytest.mark.parametrize("sql",[
    "UPDATE paper_accounts SET shared_risk_binding_sha256=NULL WHERE tenant_id='tenant'",
    "DELETE FROM paper_accounts WHERE tenant_id='tenant'",
    "UPDATE paper_shared_risk_bindings SET payload=payload",
    "DELETE FROM paper_shared_risk_bindings",
    "UPDATE shared_risk_accounts SET journal_id=journal_id",
])
def test_bound_metadata_is_immutable(sql,tmp_path):
    h=Harness(tmp_path)
    try:
        enable(h);assert h.coordinator.prepare(make_request(h.broker)).approved
        db=h.programs.db if "shared_risk_accounts" in sql else h.broker._connection
        with pytest.raises(sqlite3.IntegrityError,match="immutable"):
            db.execute(sql)
        db.rollback()
    finally:h.close()


@pytest.mark.parametrize("defect",["program","slice","bool_slice","authority","key","quantity","stop","strategy","unclaimed","premature"])
def test_broker_requires_exact_reserved_child_not_a_caller_flag(tmp_path,defect):
    h=Harness(tmp_path)
    try:
        prepared,child,evidence,key=_claimed(h,claim=defect!="unclaimed")
        if defect=="program":evidence["institutional_program"]="PROGRAM-unknown"
        elif defect=="slice":evidence["institutional_slice"]=2;key=f"{prepared.program.program_id}:2"
        elif defect=="bool_slice":evidence["institutional_slice"]=True
        elif defect=="authority":evidence["institutional_risk_authority_sha256"]="0"*64
        elif defect=="key":key="wrong-key"
        elif defect=="quantity":child=replace(child,quantity=1)
        elif defect=="stop":child=replace(child,stop_price=D(90))
        elif defect=="strategy":child=replace(child,strategy_id="different")
        elif defect=="premature":
            from datetime import timedelta
            h.broker._execution_time=NOW-timedelta(seconds=1)
        before=tuple(h.broker._connection.iterdump())
        with pytest.raises(ValueError,match="shared_risk_broker"):
            h.broker.submit_with_evidence(child,evidence,key)
        assert tuple(h.broker._connection.iterdump())==before
    finally:h.close()


def test_new_binding_does_not_configure_other_tenant_or_legacy_accounts(tmp_path):
    h=Harness(tmp_path)
    try:
        enable(h);assert h.coordinator.prepare(make_request(h.broker)).approved
        result=h.broker.buy(_direct_order("another-tenant"))
        assert result.status=="FILLED"
        assert h.broker.ledger_entries("tenant")==()
    finally:h.close()


def test_exits_do_not_need_a_working_risk_journal(tmp_path):
    h=Harness(tmp_path)
    try:
        enable(h);prepared=h.coordinator.prepare(make_request(h.broker))
        assert h.coordinator.execute_due(prepared.program.program_id,now=NOW).stage.value=="COMPLETE"
        h.programs.close();h.programs.path.rename(tmp_path/"offline-risk.sqlite")
        result=h.broker.sell(replace(prepared.approved_order,side=Side.SELL))
        assert result.status=="FILLED"
        assert h.broker.get_positions("tenant")==()
        assert len(h.broker.ledger_entries("tenant"))==2
    finally:h.close()


def test_failed_initial_journal_commit_leaves_a_hold_not_unbound_account(tmp_path,monkeypatch):
    from quant_ai.execution import institutional
    h=Harness(tmp_path)
    try:
        enable(h);pin=institutional.pin_account
        def crash(*args,**kwargs):
            pin(*args,**kwargs)
            raise RuntimeError("synthetic failure after durable pin")
        monkeypatch.setattr(institutional,"pin_account",crash)
        with pytest.raises(RuntimeError,match="synthetic"):
            h.coordinator.prepare(make_request(h.broker))
        assert h.programs.db.execute("SELECT COUNT(*) FROM execution_programs").fetchone()[0]==0
        assert h.broker._connection.execute("SELECT shared_risk_binding_sha256 FROM paper_accounts WHERE tenant_id='tenant'").fetchone()[0]
        with pytest.raises(ValueError,match="shared_risk_broker"):
            h.broker.buy(_direct_order())
        monkeypatch.setattr(institutional,"pin_account",pin)
        again=h.coordinator.prepare(make_request(h.broker))
        assert not again.approved and again.reason=="shared_risk_broker_journal_binding_missing"
    finally:h.close()


def test_old_shared_journal_has_no_automatically_invented_identity(tmp_path):
    from quant_ai.execution.shared_risk import verify_shared_risk
    h=Harness(tmp_path)
    try:
        enable(h);assert h.coordinator.prepare(make_request(h.broker)).approved
        h.programs.db.execute("ALTER TABLE shared_risk_accounts DROP COLUMN journal_id")
        h.programs.db.commit()
        assert verify_shared_risk(h.programs.db,"tenant")["journalId"] is None
        result=h.coordinator.prepare(make_request(h.broker,p=proposal(decision_id="legacy")))
        assert not result.approved and "journal_identity_mismatch" in result.reason
    finally:h.close()


def test_backup_compares_broker_anchor_without_rebinding_paths(tmp_path,monkeypatch):
    from test_institutional_state_bundle import fixture, select
    from test_shared_risk_reservations import policy

    from quant_ai.execution import institutional
    from quant_ai.operations import recovery_bundle
    original=institutional.InstitutionalPaperCoordinator.__init__
    def configured(self,*args,**kwargs):
        original(self,*args,**kwargs);self.shared_risk_policy=policy()
    monkeypatch.setattr(institutional.InstitutionalPaperCoordinator,"__init__",configured)
    spec,_=fixture(tmp_path);select(spec)
    manifest=recovery_bundle.create(spec,tmp_path/"backup",writers_stopped=True)
    report=recovery_bundle.restore(tmp_path/"backup",tmp_path/"restored",manifest_sha256=manifest["manifestSha256"])
    shared=report["institutionalRecovery"]["programs"]["sharedRisk"]
    assert shared["brokerJournalBindingVerified"] is True
    assert shared["runtimePathRebindRequired"] is True
    assert shared["activationAuthorized"] is False
    with sqlite3.connect(spec["sources"]["ledger"]) as source,sqlite3.connect(tmp_path/"restored/ledger") as restored:
        assert source.execute("SELECT payload FROM paper_shared_risk_bindings").fetchall()==restored.execute("SELECT payload FROM paper_shared_risk_bindings").fetchall()
    source.close();restored.close()
