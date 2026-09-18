"""Synthetic shared account budget; no live broker or actual risk allocation."""
from dataclasses import replace
from decimal import Decimal

import pytest
from test_institutional_paper_coordinator import NOW, Harness, make_request, proposal

from quant_ai.execution import institutional

D = Decimal


def policy():
    return institutional.SharedRiskPolicy("synthetic-account", "INR", D(".0008"), "synthetic-policy-v1")


def enable(h):
    h.coordinator.shared_risk_policy = policy()


def test_two_pending_parents_cannot_spend_the_same_account_allowance(tmp_path):
    h=Harness(tmp_path)
    try:
        enable(h)
        first=h.coordinator.prepare(make_request(h.broker))
        assert first.approved
        second=h.coordinator.prepare(make_request(h.broker,p=proposal(decision_id="second")))
        assert not second.approved and second.reason == "shared_risk_budget_exceeded"
        assert h.broker.ledger_entries("tenant") == ()
        assert len(h.programs.db.execute("SELECT * FROM execution_programs").fetchall()) == 1
    finally:h.close()


def test_completed_buy_remains_charged_while_position_is_open(tmp_path):
    h=Harness(tmp_path)
    try:
        enable(h)
        first=h.coordinator.prepare(make_request(h.broker))
        assert h.coordinator.execute_due(first.program.program_id,now=NOW).stage.value == "COMPLETE"
        request=make_request(h.broker,p=proposal(decision_id="second"))
        request=replace(request,projected_factor_positions=h.factor_provider(request.proposal,request.portfolio))
        second=h.coordinator.prepare(request)
        assert not second.approved and second.reason == "shared_risk_budget_exceeded"
        assert len(h.broker.ledger_entries("tenant")) == 1
    finally:h.close()


def test_shared_equity_shortfall_holds_entry_before_submission(tmp_path):
    h=Harness(tmp_path)
    try:
        enable(h)
        h.coordinator.shared_risk_policy=replace(policy(),max_loss_fraction=D(".0005"))
        first=h.coordinator.prepare(make_request(h.broker)); assert first.approved
        prior=h.coordinator.snapshot_provider
        h.coordinator.snapshot_provider=lambda:replace(prior(),equity=D(99000))
        result=h.coordinator.execute_due(first.program.program_id,now=NOW)
        assert result.stage.value != "COMPLETE" and result.reason == "shared_risk_budget_exceeded"
        assert h.broker.ledger_entries("tenant") == ()
    finally:h.close()


@pytest.mark.parametrize("fraction,accepted", [(".001", 2), (".000999", 1)])
def test_aggregate_budget_exact_boundary(tmp_path,fraction,accepted):
    h=Harness(tmp_path)
    try:
        enable(h);h.coordinator.shared_risk_policy=replace(policy(),max_loss_fraction=D(fraction))
        results=[h.coordinator.prepare(make_request(h.broker,p=proposal(decision_id=f"d-{i}"))) for i in range(3)]
        assert sum(r.approved for r in results)==accepted
        assert h.broker.ledger_entries("tenant")==()
    finally:h.close()


def test_racing_coordinators_share_one_capacity_decision(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    h1,h2=Harness(tmp_path),Harness(tmp_path)
    try:
        enable(h1);enable(h2);barrier=Barrier(2)
        def run(pair):
            h,index=pair
            request=make_request(h.broker,p=proposal(decision_id=f"thread-{index}"))
            barrier.wait(timeout=5)
            return h.coordinator.prepare(request)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(run,[(h1,1),(h2,2)]))
        assert sum(r.approved for r in results)==1
        assert [r.reason for r in results if not r.approved]==["shared_risk_budget_exceeded"]
        assert h1.broker.ledger_entries("tenant")==()
    finally:h2.close();h1.close()


def test_same_decision_is_reserved_once(tmp_path):
    from quant_ai.execution.shared_risk import verify_shared_risk
    h=Harness(tmp_path)
    try:
        enable(h);r=make_request(h.broker)
        first=h.coordinator.prepare(r);second=h.coordinator.prepare(r)
        assert first.approved and second.approved
        assert first.program.program_id==second.program.program_id
        state=verify_shared_risk(h.programs.db,"tenant")
        assert state["activeReservations"]==1 and state["reservedLoss"]=="50"
    finally:h.close()


def test_reservation_survives_new_connections(tmp_path):
    h=Harness(tmp_path)
    enable(h);first=h.coordinator.prepare(make_request(h.broker));assert first.approved;h.close()
    restarted=Harness(tmp_path)
    try:
        enable(restarted)
        other=restarted.coordinator.prepare(make_request(restarted.broker,p=proposal(decision_id="restart")))
        assert not other.approved and other.reason=="shared_risk_budget_exceeded"
        assert restarted.broker.ledger_entries("tenant")==()
    finally:restarted.close()


@pytest.mark.parametrize("change",["omit","account","cap","currency","revision"])
@pytest.mark.parametrize("phase",["prepare","dispatch"])
def test_existing_account_cannot_drop_or_change_its_shared_policy(tmp_path,change,phase):
    h=Harness(tmp_path)
    try:
        enable(h);first=h.coordinator.prepare(make_request(h.broker));assert first.approved
        changed={"omit":None,"account":replace(policy(),account_ref="other"),
            "cap":replace(policy(),max_loss_fraction=D(".1")),"currency":replace(policy(),currency="USD"),
            "revision":replace(policy(),revision="other")}[change]
        h.coordinator.shared_risk_policy=changed
        result=(h.coordinator.prepare(make_request(h.broker,p=proposal(decision_id="next"))) if phase=="prepare"
                else h.coordinator.execute_due(first.program.program_id,now=NOW))
        assert result.reason in {"shared_risk_configuration_required","shared_risk_configuration_changed"}
        assert h.broker.ledger_entries("tenant")==()
    finally:h.close()


def test_input_callback_cannot_switch_to_a_looser_shared_policy(tmp_path):
    h=Harness(tmp_path)
    try:
        enable(h);first=h.coordinator.prepare(make_request(h.broker))
        original=h.coordinator.factor_position_provider
        def changed(order,snapshot):
            h.coordinator.shared_risk_policy=replace(policy(),max_loss_fraction=D(".1"))
            return original(order,snapshot)
        h.coordinator.factor_position_provider=changed
        result=h.coordinator.execute_due(first.program.program_id,now=NOW)
        assert result.reason=="shared_risk_configuration_changed"
        assert h.oms.all_orders("tenant")==() and h.broker.ledger_entries("tenant")==()
    finally:h.close()


def test_cancel_never_claimed_program_releases_exactly_once(tmp_path):
    h=Harness(tmp_path)
    try:
        enable(h);first=h.coordinator.prepare(make_request(h.broker))
        a=h.coordinator.shared_risk.cancel_unclaimed(first.program.program_id,tenant_id="tenant",at=NOW)
        b=h.coordinator.shared_risk.cancel_unclaimed(first.program.program_id,tenant_id="tenant",at=NOW)
        assert a==b and a["reservedLoss"]=="0" and a["releasedReservations"]==1
        assert h.programs.get(first.program.program_id).state.value=="CANCELLED"
        new=h.coordinator.prepare(make_request(h.broker,p=proposal(decision_id="after-cancel")))
        assert new.approved
        original=h.coordinator.prepare(make_request(h.broker))
        assert not original.approved and original.reason=="shared_risk_released_decision_cannot_restart"
        assert h.broker.ledger_entries("tenant")==()
    finally:h.close()


@pytest.mark.parametrize("progress",["claimed","partial","complete"])
def test_dispatched_or_exposed_capacity_is_not_released(tmp_path,progress):
    from datetime import timedelta

    from quant_ai.execution.planner import ExecutionAlgorithm, VolumeBucket
    h=Harness(tmp_path)
    try:
        enable(h)
        request=make_request(h.broker)
        if progress=="partial":
            request=replace(request,execution_algorithm=ExecutionAlgorithm.TWAP,
                volume_buckets=(VolumeBucket(NOW,1000),VolumeBucket(NOW+timedelta(minutes=5),1000)))
        prepared=h.coordinator.prepare(request);assert prepared.approved
        if progress=="claimed":
            h.programs.claim_slice(prepared.program.program_id,1,client_order_id="synthetic-child")
        else:
            result=h.coordinator.execute_due(prepared.program.program_id,now=NOW)
            assert result.executed_sequences==(1,)
        with pytest.raises(ValueError,match="claimed_reservation_cannot_release"):
            h.coordinator.shared_risk.cancel_unclaimed(prepared.program.program_id,tenant_id="tenant",at=NOW)
    finally:h.close()


def test_covered_exit_works_without_shared_entry_configuration(tmp_path):
    from quant_ai.domain.models import Side
    from quant_ai.execution.shared_risk import verify_shared_risk
    h=Harness(tmp_path)
    try:
        enable(h);buy=h.coordinator.prepare(make_request(h.broker))
        assert h.coordinator.execute_due(buy.program.program_id,now=NOW).stage.value=="COMPLETE"
        h.coordinator.shared_risk_policy=None
        request=replace(make_request(h.broker,p=proposal(side=Side.SELL,decision_id="exit")),edge_evidence=None,projected_factor_positions=())
        sale=h.coordinator.prepare(request);assert sale.approved
        assert h.coordinator.execute_due(sale.program.program_id,now=NOW).stage.value=="COMPLETE"
        assert h.broker.get_positions("tenant")==()
        # Raw reservation history stays immutable; position-linked effective capacity is derived separately.
        assert verify_shared_risk(h.programs.db,"tenant")["reservedLoss"]=="50"
    finally:h.close()


def test_shared_mode_does_not_invent_coverage_for_legacy_history(tmp_path):
    h=Harness(tmp_path)
    try:
        legacy=h.coordinator.prepare(make_request(h.broker));assert legacy.approved
        enable(h)
        new=h.coordinator.prepare(make_request(h.broker,p=proposal(decision_id="new")))
        assert not new.approved and new.reason=="shared_risk_legacy_account_requires_migration"
    finally:h.close()


@pytest.mark.parametrize("field,value",[("max_loss_fraction",D("NaN")),("max_loss_fraction",D(-1)),
    ("max_loss_fraction",D(0)),("max_loss_fraction",D(2)),("max_loss_fraction",True),
    ("max_loss_fraction",".001"),("currency","EUR"),("account_ref",""),("revision","bad\nrevision")])
def test_invalid_policy_is_not_accepted(field,value):
    with pytest.raises((ValueError,TypeError)):
        replace(policy(),**{field:value})


@pytest.mark.parametrize("table",["shared_risk_accounts","shared_risk_reservations","shared_risk_releases"])
@pytest.mark.parametrize("action",["UPDATE","DELETE"])
def test_reservation_records_are_append_only(tmp_path,table,action):
    import sqlite3
    h=Harness(tmp_path)
    try:
        enable(h);first=h.coordinator.prepare(make_request(h.broker))
        if table=="shared_risk_releases":
            h.coordinator.shared_risk.cancel_unclaimed(first.program.program_id,tenant_id="tenant",at=NOW)
        query=(f"UPDATE {table} SET payload=payload" if action=="UPDATE" else f"DELETE FROM {table}")
        with pytest.raises(sqlite3.IntegrityError,match="immutable"):
            h.programs.db.execute(query)
    finally:h.close()


@pytest.mark.parametrize("kind",["hash","missing","release","schema"])
def test_corrupt_saved_reservations_hold_entries(tmp_path,kind):
    from quant_ai.execution.shared_risk import verify_shared_risk
    h=Harness(tmp_path)
    try:
        enable(h);first=h.coordinator.prepare(make_request(h.broker))
        if kind=="hash":
            h.programs.db.execute("DROP TRIGGER shared_risk_reservations_update_blocked")
            h.programs.db.execute("UPDATE shared_risk_reservations SET sha256='bad'")
        elif kind=="missing":
            h.programs.db.execute("DROP TRIGGER shared_risk_reservations_delete_blocked")
            h.programs.db.execute("DELETE FROM shared_risk_reservations")
        elif kind=="release":
            h.programs.db.execute("INSERT INTO shared_risk_releases VALUES(?,?,?,?)",(first.program.program_id,"tenant","{}","bad"))
        else:h.programs.db.execute("DROP TABLE shared_risk_releases")
        h.programs.db.commit()
        with pytest.raises(ValueError):verify_shared_risk(h.programs.db,"tenant")
        result=h.coordinator.execute_due(first.program.program_id,now=NOW)
        assert result.stage.value=="FAILED" and h.broker.ledger_entries("tenant")==()
    finally:h.close()


def test_exception_after_reservation_rolls_back_parent_and_budget(tmp_path,monkeypatch):
    class Crash(BaseException):pass
    h=Harness(tmp_path)
    try:
        enable(h);original=h.coordinator.shared_risk.reserve
        def crash(*args,**kwargs):
            original(*args,**kwargs);raise Crash()
        monkeypatch.setattr(h.coordinator.shared_risk,"reserve",crash)
        with pytest.raises(Crash):h.coordinator.prepare(make_request(h.broker))
        assert h.programs.db.execute("SELECT COUNT(*) FROM execution_programs").fetchone()[0]==0
        assert h.broker.ledger_entries("tenant")==()
    finally:h.close()


@pytest.mark.parametrize("committed",[False,True])
def test_process_death_cannot_split_parent_and_risk_charge(tmp_path,committed,monkeypatch):
    import os
    import subprocess
    import sys
    from pathlib import Path

    from quant_ai.execution.program import ExecutionProgramJournal
    from quant_ai.execution.shared_risk import verify_shared_risk
    monkeypatch.delenv("PYTHONPATH",raising=False)
    root=Path(__file__).resolve().parents[1]
    code="""
import os,sys
from pathlib import Path
from test_shared_risk_reservations import enable
from test_institutional_paper_coordinator import Harness,make_request
h=Harness(Path(sys.argv[1]));enable(h)
if sys.argv[2]=='uncommitted':
    original=h.coordinator.shared_risk.reserve
    def interrupt(*args,**kwargs):
        original(*args,**kwargs);os._exit(74)
    h.coordinator.shared_risk.reserve=interrupt
h.coordinator.prepare(make_request(h.broker));os._exit(74)
"""
    result=subprocess.run([sys.executable,"-c",code,str(tmp_path),"committed" if committed else "uncommitted"],
        env={**os.environ,"PYTHONPATH":str(root/"src")+os.pathsep+str(root/"tests"),"TRADING_LIVE_MONEY_ACTIVE":"false"},capture_output=True,timeout=30,check=False)
    assert result.returncode==74,result.stderr.decode()
    with ExecutionProgramJournal(tmp_path/"programs.sqlite") as programs:
        count=programs.db.execute("SELECT COUNT(*) FROM execution_programs").fetchone()[0]
        assert count==int(committed)
        state=verify_shared_risk(programs.db,"tenant")
        assert state["reservedLoss"]==("50" if committed else "0")


def test_offline_bundle_replays_shared_reservations(tmp_path,monkeypatch):
    from test_institutional_state_bundle import fixture, select

    from quant_ai.operations import recovery_bundle
    original=institutional.InstitutionalPaperCoordinator.__init__
    def initialize(self,*args,**kwargs):
        original(self,*args,**kwargs);self.shared_risk_policy=policy()
    monkeypatch.setattr(institutional.InstitutionalPaperCoordinator,"__init__",initialize)
    spec,_=fixture(tmp_path);select(spec)
    manifest=recovery_bundle.create(spec,tmp_path/"bundle",writers_stopped=True)
    report=recovery_bundle.restore(tmp_path/"bundle",tmp_path/"restore",manifest_sha256=manifest["manifestSha256"])
    risk=report["institutionalRecovery"]["programs"]["sharedRisk"]
    assert risk["activeReservations"]==1 and risk["reservedLoss"]=="50"
    assert risk["activationAuthorized"] is False


def test_rehashed_lower_charge_cannot_hide_declared_adverse_payoff(tmp_path):
    import hashlib
    import json

    from quant_ai.execution.shared_risk import verify_shared_risk
    h=Harness(tmp_path)
    try:
        enable(h);h.coordinator.shared_risk_policy=replace(policy(),max_loss_fraction=D(".001"))
        request=make_request(h.broker)
        request=replace(request,edge_evidence=replace(request.edge_evidence,average_win_return=D(".2"),average_loss_return=D(".08"),expected_cost_return=D(".005")))
        assert h.coordinator.prepare(request).approved
        row=h.programs.db.execute("SELECT payload FROM shared_risk_reservations").fetchone()
        body=json.loads(row[0]);body["amount"]="50"
        raw=json.dumps(body,sort_keys=True,separators=(",",":"))
        h.programs.db.execute("DROP TRIGGER shared_risk_reservations_update_blocked")
        h.programs.db.execute("UPDATE shared_risk_reservations SET payload=?,sha256=?",(raw,hashlib.sha256(raw.encode()).hexdigest()))
        h.programs.db.commit()
        with pytest.raises(ValueError,match="reservation_model_mismatch"):
            verify_shared_risk(h.programs.db,"tenant")
    finally:h.close()


def test_cancel_cannot_cross_tenant_boundary(tmp_path):
    h=Harness(tmp_path)
    try:
        enable(h);first=h.coordinator.prepare(make_request(h.broker))
        with pytest.raises(ValueError,match="tenant_mismatch"):
            h.coordinator.shared_risk.cancel_unclaimed(first.program.program_id,tenant_id="other",at=NOW)
        assert h.programs.get(first.program.program_id).state.value=="PLANNED"
    finally:h.close()


def test_distinct_strategies_share_the_account_cap(tmp_path):
    h=Harness(tmp_path)
    try:
        enable(h);assert h.coordinator.prepare(make_request(h.broker)).approved
        request=make_request(h.broker,p=proposal(decision_id="other-strategy"))
        request=replace(request,strategy_id="second-strategy",
            strategy_opportunities=(replace(request.strategy_opportunities[0],strategy_id="second-strategy"),))
        result=h.coordinator.prepare(request)
        assert not result.approved and result.reason=="shared_risk_budget_exceeded"
    finally:h.close()


def test_preparation_callback_cannot_choose_a_different_initial_account_policy(tmp_path):
    h=Harness(tmp_path)
    try:
        enable(h);original=h.coordinator.strategy_exposure_provider
        def changed(strategy):
            h.coordinator.shared_risk_policy=replace(policy(),max_loss_fraction=D(".1"))
            return original(strategy)
        h.coordinator.strategy_exposure_provider=changed
        result=h.coordinator.prepare(make_request(h.broker))
        assert not result.approved and result.reason=="shared_risk_configuration_changed"
        assert h.programs.db.execute("SELECT COUNT(*) FROM execution_programs").fetchone()[0]==0
    finally:h.close()
