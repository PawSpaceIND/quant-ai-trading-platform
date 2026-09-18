"""Synthetic refusal checks for stale risk journals; no capacity release or live calls."""
import sqlite3
from dataclasses import replace

import pytest
from test_institutional_paper_coordinator import NOW, Harness, make_request, proposal
from test_shared_risk_reservations import enable

from quant_ai.execution.shared_risk_binding import verify_binding_pair


def restored_older_journal(tmp_path):
    h=Harness(tmp_path)
    enable(h)
    initial=h.coordinator.prepare(make_request(h.broker,p=proposal(decision_id="unused")))
    assert initial.approved
    h.coordinator.shared_risk.cancel_unclaimed(initial.program.program_id,tenant_id="tenant",at=NOW)
    old=sqlite3.connect(tmp_path/"historical-journal.sqlite")
    h.programs.db.backup(old)
    bought=h.coordinator.prepare(make_request(h.broker,p=proposal(decision_id="committed")))
    assert bought.approved
    assert h.coordinator.execute_due(bought.program.program_id,now=NOW).stage.value=="COMPLETE"
    old.backup(h.programs.db)
    old.close()
    return h,bought


def test_older_same_identity_journal_cannot_admit_new_risk_after_committed_fill(tmp_path):
    h,_=restored_older_journal(tmp_path)
    try:
        before=tuple(h.programs.db.iterdump())
        request=make_request(h.broker,p=proposal(decision_id="after-rollback"))
        request=replace(request,projected_factor_positions=h.factor_provider(request.proposal,request.portfolio))
        result=h.coordinator.prepare(request)
        assert not result.approved and "shared_risk_broker_committed_entry" in result.reason
        assert tuple(h.programs.db.iterdump())==before
        assert len(h.broker.ledger_entries("tenant"))==1
    finally:h.close()


def test_binding_pair_rejects_missing_program_despite_same_uuid_policy_and_path(tmp_path):
    h,_=restored_older_journal(tmp_path)
    try:
        with pytest.raises(ValueError,match="shared_risk_broker_committed_entry"):
            verify_binding_pair(h.broker._connection,h.programs.db,"tenant")
        assert len(h.broker.ledger_entries("tenant"))==1
    finally:h.close()


def test_final_broker_refuses_a_valid_pending_child_when_other_committed_history_was_lost(tmp_path):
    from decimal import Decimal

    from test_shared_risk_reservations import policy

    from quant_ai.orders.oms import DurableOms
    h=Harness(tmp_path)
    old=sqlite3.connect(tmp_path/"before-committed-buy.sqlite")
    try:
        h.coordinator.shared_risk_policy=replace(policy(),max_loss_fraction=Decimal(".001"))
        queued=h.coordinator.prepare(make_request(h.broker,p=proposal(decision_id="queued")))
        assert queued.approved
        h.programs.db.backup(old)
        bought=h.coordinator.prepare(make_request(h.broker,p=proposal(decision_id="bought")))
        assert bought.approved
        assert h.coordinator.execute_due(bought.program.program_id,now=NOW).stage.value=="COMPLETE"
        old.backup(h.programs.db)
        child=queued.approved_order
        pid=queued.program.program_id
        assert h.programs.claim_slice(pid,1,client_order_id=DurableOms.client_order_id(child,"queued:slice:1"))
        evidence={"schema":"pramana.swarm_fill.v1","event_type":"swarm_fill",
            "institutional_program":pid,"institutional_slice":1,
            "institutional_risk_authority_sha256":queued.program.runtime_context_sha256}
        before=tuple(h.broker._connection.iterdump())
        with pytest.raises(ValueError,match="shared_risk_broker_committed_entry"):
            h.broker.submit_with_evidence(child,evidence,f"{pid}:1")
        assert tuple(h.broker._connection.iterdump())==before
    finally:old.close();h.close()


def _completed(tmp_path):
    h=Harness(tmp_path)
    enable(h)
    prepared=h.coordinator.prepare(make_request(h.broker))
    assert h.coordinator.execute_due(prepared.program.program_id,now=NOW).stage.value=="COMPLETE"
    return h,prepared


def _corrupt(db,table,sql,args=()):
    # Disposable negative-test database only; never change running account records.
    for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?",(table,)).fetchall():
        assert name.replace("_","").isalnum()
        db.execute(f'DROP TRIGGER "{name}"')
    db.execute(sql,args)
    db.commit()


@pytest.mark.parametrize("defect",["missing_receipt","wrong_program","wrong_slice","boolean_slice",
    "missing_pin","wrong_authority","wrong_intent","wrong_key","wrong_price","boolean_quantity",
    "duplicate_field","nan","missing_claim","changed_client","reset_slice"])
def test_historical_entry_corruption_refuses_without_mutating_stores(tmp_path,defect):
    import json
    h,_prepared=_completed(tmp_path)
    try:
        ledger=h.broker._connection
        row=ledger.execute("SELECT payload FROM paper_decision_evidence").fetchone()
        payload=json.loads(row[0])
        if defect=="missing_receipt":
            _corrupt(ledger,"paper_decision_evidence","DELETE FROM paper_decision_evidence")
        elif defect=="missing_claim":
            _corrupt(ledger,"paper_idempotency","DELETE FROM paper_idempotency")
        elif defect in {"changed_client","reset_slice"}:
            field,value=("client_order_id","other") if defect=="changed_client" else ("state","PENDING")
            _corrupt(h.programs.db,"execution_program_slices",f"UPDATE execution_program_slices SET {field}=?",(value,))
        else:
            if defect=="wrong_program":payload["institutional_program"]="absent"
            elif defect=="wrong_slice":payload["institutional_slice"]=2
            elif defect=="boolean_slice":payload["institutional_slice"]=True
            elif defect=="missing_pin":payload.pop("shared_risk_binding_sha256")
            elif defect=="wrong_authority":payload["institutional_risk_authority_sha256"]="b"*64
            elif defect=="wrong_intent":payload["paper_submission_receipt"]["orderIntent"]="{}"
            elif defect=="wrong_key":payload["idempotency_key"]="different"
            elif defect=="wrong_price":payload["fill"]["price"]="101"
            elif defect=="boolean_quantity":payload["fill"]["quantity"]=True
            raw=json.dumps(payload)
            if defect=="duplicate_field":raw=raw[:-1]+',"institutional_slice":1}'
            if defect=="nan":raw=raw[:-1]+',"unexpected":NaN}'
            _corrupt(ledger,"paper_decision_evidence","UPDATE paper_decision_evidence SET payload=?",(raw,))
        before=(tuple(ledger.iterdump()),tuple(h.programs.db.iterdump()))
        with pytest.raises(ValueError,match="shared_risk_broker_committed_entry"):
            verify_binding_pair(ledger,h.programs.db,"tenant")
        assert (tuple(ledger.iterdump()),tuple(h.programs.db.iterdump()))==before
    finally:h.close()


def test_committed_but_unrecorded_fill_retains_risk_and_recovers_normally(tmp_path,monkeypatch):
    from quant_ai.execution.shared_risk import verify_shared_risk
    h=Harness(tmp_path)
    try:
        enable(h)
        prepared=h.coordinator.prepare(make_request(h.broker))
        original=h.programs.mark_filled_unaccounted
        def interrupted(*args,**kwargs):
            raise RuntimeError("synthetic interruption after paper commit")
        monkeypatch.setattr(h.programs,"mark_filled_unaccounted",interrupted)
        with pytest.raises(RuntimeError,match="synthetic interruption"):
            h.coordinator.execute_due(prepared.program.program_id,now=NOW)
        assert len(h.broker.ledger_entries("tenant"))==1
        assert h.programs.get(prepared.program.program_id).slices[0].state.value=="DISPATCHING"
        assert verify_binding_pair(h.broker._connection,h.programs.db,"tenant") is not None
        assert verify_shared_risk(h.programs.db,"tenant")["reservedLoss"]=="50"
        monkeypatch.setattr(h.programs,"mark_filled_unaccounted",original)
        assert h.coordinator.reconcile_accounting(prepared.program.program_id).stage.value=="COMPLETE"
        assert len(h.broker.ledger_entries("tenant"))==1
        assert verify_shared_risk(h.programs.db,"tenant")["reservedLoss"]=="50"
    finally:h.close()


def test_same_journal_restart_preserves_committed_entry_coverage(tmp_path):
    h,_=_completed(tmp_path)
    pin=verify_binding_pair(h.broker._connection,h.programs.db,"tenant")
    h.close()
    restarted=Harness(tmp_path)
    try:
        assert verify_binding_pair(restarted.broker._connection,restarted.programs.db,"tenant")==pin
        assert len(restarted.broker.ledger_entries("tenant"))==1
    finally:restarted.close()


def test_independent_protective_exit_survives_incomplete_restored_journal(tmp_path):
    from decimal import Decimal

    from quant_ai.execution.protective_exits import ProtectiveExitEngine
    h,_=restored_older_journal(tmp_path)
    try:
        outcomes=ProtectiveExitEngine(h.broker,lambda _:Decimal(90),tenant_id="tenant").evaluate()
        assert len(outcomes)==1 and outcomes[0].filled
        assert h.broker.get_positions("tenant")==()
        with pytest.raises(ValueError,match="shared_risk_broker_committed_entry"):
            verify_binding_pair(h.broker._connection,h.programs.db,"tenant")
    finally:h.close()


@pytest.mark.parametrize("check_paths",[True,False])
def test_offline_path_exemption_never_exempts_committed_history(tmp_path,check_paths):
    h,_=restored_older_journal(tmp_path)
    try:
        with pytest.raises(ValueError,match="shared_risk_broker_committed_entry"):
            verify_binding_pair(h.broker._connection,h.programs.db,"tenant",check_paths=check_paths)
    finally:h.close()


def test_replay_inventory_overflow_refuses_instead_of_truncating(tmp_path,monkeypatch):
    from quant_ai.execution import shared_risk_binding
    h,_=_completed(tmp_path)
    try:
        monkeypatch.setattr(shared_risk_binding,"MAX_COMMITTED_ENTRIES",0)
        with pytest.raises(ValueError,match="committed_entry_inventory_limit"):
            verify_binding_pair(h.broker._connection,h.programs.db,"tenant")
    finally:h.close()


def test_valid_offline_bundle_replays_the_added_entry_coverage(tmp_path,monkeypatch):
    from test_institutional_state_bundle import fixture, select
    from test_shared_risk_reservations import policy

    from quant_ai.execution.institutional import InstitutionalPaperCoordinator
    from quant_ai.operations import recovery_bundle
    original=InstitutionalPaperCoordinator.__init__
    def initialize(self,*args,**kwargs):
        original(self,*args,**kwargs)
        self.shared_risk_policy=policy()
    monkeypatch.setattr(InstitutionalPaperCoordinator,"__init__",initialize)
    spec,_=fixture(tmp_path)
    select(spec)
    manifest=recovery_bundle.create(spec,tmp_path/"bundle",writers_stopped=True)
    result=recovery_bundle.restore(tmp_path/"bundle",tmp_path/"restored",manifest_sha256=manifest["manifestSha256"])
    assert result["status"]=="restored"
    assert result["institutionalRecovery"]["programs"]["sharedRisk"]["brokerJournalBindingVerified"] is True
    assert result["institutionalRecovery"]["activationAuthorized"] is False
