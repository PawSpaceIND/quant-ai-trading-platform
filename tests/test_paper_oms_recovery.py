"""Recover committed synthetic paper fills only; never dispatch an order or clear a halt."""
from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from types import SimpleNamespace

import pytest
from test_runtime_contract_mode import build, close, submit_bound

from quant_ai.domain.models import Side
from quant_ai.orders.state import OrderState


def interrupted_runner(tmp_path, monkeypatch, *, committed=True):
    monkeypatch.setenv("PRAMANA_RELEASE_REVISION", "a" * 40)
    runner = build(tmp_path)
    runtime = runner.daemon.scheduler.pipeline.runtime
    original = runtime.broker.submit_with_evidence
    def interrupted(*args, **kwargs):
        if committed:
            original(*args, **kwargs)
        raise ValueError("synthetic_submit_interruption")
    with monkeypatch.context() as patch:
        patch.setattr(runtime.broker, "submit_with_evidence", interrupted)
        result = submit_bound(runner, "recovery-test")
    assert result.order_state is OrderState.SUBMISSION_UNCERTAIN
    assert runner.daemon._pilot_pre_submit(SimpleNamespace(side=Side.BUY)) == "runtime_identity_oms_recovery_required"
    return runner


def recovery_for(runner):
    from quant_ai.orders.paper_recovery import PaperOmsRecovery
    runtime = runner.daemon.scheduler.pipeline.runtime
    return PaperOmsRecovery(broker=runtime.broker, oms=runtime.oms)


def test_committed_fill_recovers_with_no_new_trade_and_halt_retained(tmp_path, monkeypatch):
    runner = interrupted_runner(tmp_path, monkeypatch)
    try:
        runtime = runner.daemon.scheduler.pipeline.runtime
        cid = runtime.oms.all_orders("pilot")[0].client_order_id
        before = tuple(runtime.broker._connection.iterdump())
        recovery = recovery_for(runner)
        plan = recovery.inspect(cid, tenant_id="pilot")
        assert plan.status == "READY"
        assert tuple(runtime.broker._connection.iterdump()) == before
        with monkeypatch.context() as patch:
            patch.setattr(runtime.broker, "submit_with_evidence", lambda *a, **k: pytest.fail("must not submit"))
            result = recovery.recover(cid, tenant_id="pilot", reviewed_plan_sha256=plan.sha256, reviewer="synthetic-reviewer")
        assert result.status == "MATCHED"
        assert runtime.oms.get(cid).state is OrderState.FILLED
        assert runtime.oms.verify(cid)["verified"]
        assert tuple(runtime.broker._connection.iterdump()) == before
        assert runner.daemon.kill_switch.engaged
        rows = tuple(runtime.oms.db.iterdump())
        assert recovery.recover(cid, tenant_id="pilot", reviewed_plan_sha256=plan.sha256, reviewer="synthetic-reviewer").status == "MATCHED"
        assert tuple(runtime.oms.db.iterdump()) == rows
    finally:
        close(runner)


def test_absent_receipt_is_not_permission_to_retry_or_reject(tmp_path, monkeypatch):
    runner = interrupted_runner(tmp_path, monkeypatch, committed=False)
    try:
        runtime = runner.daemon.scheduler.pipeline.runtime
        cid = runtime.oms.all_orders("pilot")[0].client_order_id
        plan = recovery_for(runner).inspect(cid, tenant_id="pilot")
        assert plan.status == "BLOCKED" and plan.reason == "paper_recovery_receipt_unavailable"
        before = tuple(runtime.oms.db.iterdump())
        with pytest.raises(ValueError, match="receipt_unavailable"):
            recovery_for(runner).recover(cid, tenant_id="pilot", reviewed_plan_sha256=plan.sha256, reviewer="synthetic-reviewer")
        assert tuple(runtime.oms.db.iterdump()) == before
        assert not runtime.broker.ledger_entries("pilot")
        assert runtime.oms.get(cid).state is OrderState.SUBMISSION_UNCERTAIN
    finally:
        close(runner)


def test_recovery_requires_exact_reviewed_snapshot(tmp_path, monkeypatch):
    runner = interrupted_runner(tmp_path, monkeypatch)
    try:
        runtime = runner.daemon.scheduler.pipeline.runtime
        cid = runtime.oms.all_orders("pilot")[0].client_order_id
        before = tuple(runtime.oms.db.iterdump())
        with pytest.raises(ValueError, match="reviewed_plan"):
            recovery_for(runner).recover(cid, tenant_id="pilot", reviewed_plan_sha256="f" * 64, reviewer="synthetic-reviewer")
        assert tuple(runtime.oms.db.iterdump()) == before
    finally:
        close(runner)


def identifiers(runner):
    runtime = runner.daemon.scheduler.pipeline.runtime
    cid = runtime.oms.all_orders("pilot")[0].client_order_id
    return runtime, cid


def damage_receipt(runtime, change):
    db = runtime.broker._connection
    with db:
        for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='paper_decision_evidence'").fetchall():
            assert row[0].replace("_", "").isalnum()
            db.execute(f'DROP TRIGGER "{row[0]}"')
        row = db.execute("SELECT order_id,payload FROM paper_decision_evidence").fetchone()
        payload = json.loads(row[1])
        change(payload)
        db.execute("UPDATE paper_decision_evidence SET payload=? WHERE order_id=?", (json.dumps(payload), row[0]))


def test_restart_recovery_is_idempotent_and_never_clears_persisted_halt(tmp_path, monkeypatch):
    runner = interrupted_runner(tmp_path, monkeypatch)
    runtime, cid = identifiers(runner)
    plan = recovery_for(runner).inspect(cid, tenant_id="pilot")
    close(runner)
    runner = build(tmp_path)
    try:
        recovery = recovery_for(runner)
        assert recovery.recover(cid, tenant_id="pilot", reviewed_plan_sha256=plan.sha256, reviewer="synthetic-reviewer").status == "MATCHED"
        runtime, cid = identifiers(runner)
        assert runner.daemon.kill_switch.engaged
        assert len(runtime.broker.ledger_entries("pilot")) == 1
        assert runtime.oms.db.execute("SELECT COUNT(*) FROM oms_paper_recoveries").fetchone()[0] == 1
        assert runner.daemon.tracker.risk_state.kill_switch_state("pilot")[0]
    finally:
        close(runner)


def test_audit_failure_rolls_back_fill_and_all_schema_changes(tmp_path, monkeypatch):
    runner = interrupted_runner(tmp_path, monkeypatch)
    try:
        runtime, cid = identifiers(runner)
        recovery = recovery_for(runner)
        plan = recovery.inspect(cid, tenant_id="pilot")
        before = tuple(runtime.oms.db.iterdump())
        def fail(*args):
            raise RuntimeError("synthetic_audit_failure")
        with monkeypatch.context() as patch:
            patch.setattr(recovery, "_append_record", fail)
            with pytest.raises(RuntimeError, match="synthetic_audit_failure"):
                recovery.recover(cid, tenant_id="pilot", reviewed_plan_sha256=plan.sha256, reviewer="synthetic-reviewer")
        assert tuple(runtime.oms.db.iterdump()) == before
        assert recovery.recover(cid, tenant_id="pilot", reviewed_plan_sha256=plan.sha256, reviewer="synthetic-reviewer").status == "MATCHED"
    finally:
        close(runner)


def test_receipt_metadata_change_invalidates_previously_reviewed_plan(tmp_path, monkeypatch):
    runner = interrupted_runner(tmp_path, monkeypatch)
    try:
        runtime, cid = identifiers(runner)
        recovery = recovery_for(runner)
        plan = recovery.inspect(cid, tenant_id="pilot")
        damage_receipt(runtime, lambda p: p.update(declared_rationales=["different synthetic evidence"]))
        assert recovery.inspect(cid, tenant_id="pilot").status == "READY"
        with pytest.raises(ValueError, match="reviewed_plan_changed"):
            recovery.recover(cid, tenant_id="pilot", reviewed_plan_sha256=plan.sha256, reviewer="synthetic-reviewer")
        assert runtime.oms.get(cid).state is OrderState.SUBMISSION_UNCERTAIN
    finally:
        close(runner)


@pytest.mark.parametrize("defect", ["stop", "decision", "approval", "key", "missing_receipt", "fill_price", "release_status", "manifest"])
def test_changed_or_missing_execution_authority_cannot_recover(tmp_path, monkeypatch, defect):
    runner = interrupted_runner(tmp_path, monkeypatch)
    try:
        runtime, cid = identifiers(runner)
        def change(payload):
            if defect == "stop":
                intent = json.loads(payload["paper_submission_receipt"]["orderIntent"])
                intent["stopPrice"] = "80"
                payload["paper_submission_receipt"]["orderIntent"] = json.dumps(intent, sort_keys=True, separators=(",", ":"))
            elif defect == "decision":
                payload["decision_id"] = "different-decision"
            elif defect == "approval":
                payload["risk_verdict"]["approved"] = "false"
            elif defect == "key":
                payload["idempotency_key"] = "f" * 64
            elif defect == "missing_receipt":
                del payload["paper_submission_receipt"]
            elif defect == "fill_price":
                payload["fill"]["price"] = "999"
            elif defect == "release_status":
                payload["provenance"]["runtime_strategy"]["status"] = "incomplete"
            else:
                payload["provenance"]["runtime_strategy"]["sha256"] = "f" * 64
        damage_receipt(runtime, change)
        before = tuple(runtime.oms.db.iterdump())
        plan = recovery_for(runner).inspect(cid, tenant_id="pilot")
        assert plan.status == "BLOCKED"
        with pytest.raises(ValueError, match="paper_recovery_"):
            recovery_for(runner).recover(cid, tenant_id="pilot", reviewed_plan_sha256=plan.sha256, reviewer="synthetic-reviewer")
        assert tuple(runtime.oms.db.iterdump()) == before
    finally:
        close(runner)


@pytest.mark.parametrize("reviewer", ["", "   ", "bad\nidentity", "bad\u2028identity", "x" * 257, None])
def test_invalid_review_identity_never_writes(tmp_path, monkeypatch, reviewer):
    runner = interrupted_runner(tmp_path, monkeypatch)
    try:
        runtime, cid = identifiers(runner)
        recovery = recovery_for(runner)
        plan = recovery.inspect(cid, tenant_id="pilot")
        before = tuple(runtime.oms.db.iterdump())
        with pytest.raises(ValueError, match="reviewer_invalid"):
            recovery.recover(cid, tenant_id="pilot", reviewed_plan_sha256=plan.sha256, reviewer=reviewer)
        assert tuple(runtime.oms.db.iterdump()) == before
    finally:
        close(runner)


def test_no_recovery_without_existing_durable_halt(tmp_path, monkeypatch):
    runner = interrupted_runner(tmp_path, monkeypatch)
    try:
        runtime, cid = identifiers(runner)
        recovery = recovery_for(runner)
        plan = recovery.inspect(cid, tenant_id="pilot")
        with runtime.broker._connection as db:
            db.execute("UPDATE risk_control_state SET kill_switch_engaged=0 WHERE tenant_id='pilot'")
        with pytest.raises(ValueError, match="durable_halt_required"):
            recovery.recover(cid, tenant_id="pilot", reviewed_plan_sha256=plan.sha256, reviewer="synthetic-reviewer")
        assert not runtime.oms.fill_ids(cid)
    finally:
        close(runner)


def test_wrong_tenant_has_no_access_to_receipt_details(tmp_path, monkeypatch):
    runner = interrupted_runner(tmp_path, monkeypatch)
    try:
        _, cid = identifiers(runner)
        plan = recovery_for(runner).inspect(cid, tenant_id="another-tenant")
        assert plan.status == "BLOCKED"
        assert plan.reason == "paper_recovery_tenant_mismatch"
        assert "receipt_sha256" not in json.loads(plan.payload)
        assert "price" not in json.loads(plan.payload)
    finally:
        close(runner)


def test_future_dated_execution_is_not_recovered(tmp_path, monkeypatch):
    runner = interrupted_runner(tmp_path, monkeypatch)
    try:
        runtime, cid = identifiers(runner)
        early = runtime.broker.ledger_entries("pilot")[0].created_at - timedelta(seconds=1)
        plan = recovery_for(runner).inspect(cid, tenant_id="pilot", now=early)
        assert plan.status == "BLOCKED" and plan.reason == "paper_recovery_execution_time_invalid"
    finally:
        close(runner)


def test_recovery_after_independent_exit_does_not_reopen_or_retrade(tmp_path, monkeypatch):
    from test_pilot_closure import publish_tick
    from test_runtime_contract_mode import NOW
    runner = interrupted_runner(tmp_path, monkeypatch)
    try:
        runtime, cid = identifiers(runner)
        publish_tick(runner, "90", NOW)
        runner.daemon.protection_tick(NOW)
        assert not runtime.broker.get_positions("pilot")
        assert len(runtime.broker.ledger_entries("pilot")) == 2
        before = tuple(runtime.broker._connection.iterdump())
        recovery = recovery_for(runner)
        plan = recovery.inspect(cid, tenant_id="pilot")
        assert recovery.recover(cid, tenant_id="pilot", reviewed_plan_sha256=plan.sha256, reviewer="synthetic-reviewer").status == "MATCHED"
        assert tuple(runtime.broker._connection.iterdump()) == before
        assert runner.daemon.kill_switch.engaged
    finally:
        close(runner)


def test_recovery_audit_is_append_only(tmp_path, monkeypatch):
    runner = interrupted_runner(tmp_path, monkeypatch)
    try:
        runtime, cid = identifiers(runner)
        recovery = recovery_for(runner)
        plan = recovery.inspect(cid, tenant_id="pilot")
        recovery.recover(cid, tenant_id="pilot", reviewed_plan_sha256=plan.sha256, reviewer="synthetic-reviewer")
        for sql in ("UPDATE oms_paper_recoveries SET payload='{}'", "DELETE FROM oms_paper_recoveries"):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"), runtime.oms.db:
                runtime.oms.db.execute(sql)
    finally:
        close(runner)


def test_recovery_validation_failure_after_append_rolls_everything_back(tmp_path, monkeypatch):
    runner = interrupted_runner(tmp_path, monkeypatch)
    try:
        runtime, cid = identifiers(runner)
        recovery = recovery_for(runner)
        plan = recovery.inspect(cid, tenant_id="pilot")
        before = tuple(runtime.oms.db.iterdump())
        original = recovery._append_record
        def corrupted(cid, tenant, payload):
            original(cid, tenant, {**payload, "after_oms_head": "f" * 64})
        with monkeypatch.context() as patch:
            patch.setattr(recovery, "_append_record", corrupted)
            with pytest.raises(ValueError, match="audit_mismatch"):
                recovery.recover(cid, tenant_id="pilot", reviewed_plan_sha256=plan.sha256, reviewer="synthetic-reviewer")
        assert tuple(runtime.oms.db.iterdump()) == before
    finally:
        close(runner)


@pytest.mark.parametrize("stage", ["before_audit", "after_commit"])
def test_abrupt_process_termination_cannot_duplicate_or_half_commit_recovery(tmp_path, monkeypatch, stage):
    import os
    import subprocess
    import sys
    from pathlib import Path
    runner = interrupted_runner(tmp_path, monkeypatch)
    try:
        runtime, cid = identifiers(runner)
        recovery = recovery_for(runner)
        plan = recovery.inspect(cid, tenant_id="pilot")
        before_broker = tuple(runtime.broker._connection.iterdump())
        before_oms = tuple(runtime.oms.db.iterdump())
        child = """
import os, sys
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.orders.oms import DurableOms
from quant_ai.orders.paper_recovery import PaperOmsRecovery
broker = PaperBrokerService(sys.argv[1])
oms = DurableOms(sys.argv[2])
recovery = PaperOmsRecovery(broker=broker, oms=oms)
if sys.argv[5] == 'before_audit':
    recovery._append_record = lambda *args: os._exit(72)
recovery.recover(sys.argv[3], tenant_id='pilot', reviewed_plan_sha256=sys.argv[4], reviewer='synthetic-reviewer')
os._exit(73)
"""
        result = subprocess.run([sys.executable, "-c", child, str(tmp_path / "ledger.db"),
            str(tmp_path / "oms.sqlite"), cid, plan.sha256, stage],
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                 "TRADING_LIVE_MONEY_ACTIVE": "false"}, capture_output=True, text=True, timeout=15, check=False)
        assert result.returncode == (72 if stage == "before_audit" else 73), result.stderr
        if stage == "before_audit":
            assert tuple(runtime.oms.db.iterdump()) == before_oms
        assert tuple(runtime.broker._connection.iterdump()) == before_broker
        assert recovery.recover(cid, tenant_id="pilot", reviewed_plan_sha256=plan.sha256, reviewer="synthetic-reviewer").status == "MATCHED"
        assert runtime.oms.fill_ids(cid) == (runtime.broker.ledger_entries("pilot")[0].order_id,)
        assert runtime.oms.db.execute("SELECT COUNT(*) FROM oms_paper_recoveries").fetchone()[0] == 1
        assert tuple(runtime.broker._connection.iterdump()) == before_broker
    finally:
        close(runner)


def test_two_connections_recover_one_fill_exactly_once(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from quant_ai.execution.paper_ledger import PaperBrokerService
    from quant_ai.orders.oms import DurableOms
    from quant_ai.orders.paper_recovery import PaperOmsRecovery
    runner = interrupted_runner(tmp_path, monkeypatch)
    other_broker = PaperBrokerService(tmp_path / "ledger.db")
    other_oms = DurableOms(tmp_path / "oms.sqlite")
    try:
        runtime, cid = identifiers(runner)
        first = recovery_for(runner)
        second = PaperOmsRecovery(broker=other_broker, oms=other_oms)
        plan = first.inspect(cid, tenant_id="pilot")
        assert second.inspect(cid, tenant_id="pilot").sha256 == plan.sha256
        def apply(service):
            return service.recover(cid, tenant_id="pilot", reviewed_plan_sha256=plan.sha256,
                                   reviewer="synthetic-reviewer").status
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(apply, (first, second)))
        assert results == ["MATCHED", "MATCHED"]
        assert runtime.oms.db.execute("SELECT COUNT(*) FROM oms_paper_recoveries").fetchone()[0] == 1
        assert len(runtime.oms.fill_ids(cid)) == 1
        assert len(runtime.broker.ledger_entries("pilot")) == 1
    finally:
        other_oms.close()
        other_broker.close()
        close(runner)


def test_order_left_submitted_after_fill_recording_error_is_recoverable(tmp_path, monkeypatch):
    monkeypatch.setenv("PRAMANA_RELEASE_REVISION", "a" * 40)
    runner = build(tmp_path)
    try:
        runtime = runner.daemon.scheduler.pipeline.runtime
        def fail(*a, **k):
            raise RuntimeError("synthetic_oms_unavailable")
        with monkeypatch.context() as patch:
            patch.setattr(runtime.oms, "fill", fail)
            with pytest.raises(RuntimeError, match="oms_unavailable"):
                submit_bound(runner, "submitted-not-recorded")
        cid = runtime.oms.all_orders("pilot")[0].client_order_id
        assert runtime.oms.get(cid).state is OrderState.SUBMITTED
        assert runner.daemon._pilot_pre_submit(SimpleNamespace(side=Side.BUY)) == "runtime_identity_oms_recovery_required"
        recovery = recovery_for(runner)
        plan = recovery.inspect(cid, tenant_id="pilot")
        assert recovery.recover(cid, tenant_id="pilot", reviewed_plan_sha256=plan.sha256, reviewer="synthetic-reviewer").status == "MATCHED"
        assert len(runtime.broker.ledger_entries("pilot")) == 1
    finally:
        close(runner)


@pytest.mark.parametrize("state", [OrderState.CANCELLED, OrderState.REJECTED])
def test_conflicting_terminal_state_is_not_rewritten(tmp_path, monkeypatch, state):
    runner = interrupted_runner(tmp_path, monkeypatch)
    try:
        runtime, cid = identifiers(runner)
        runtime.oms.transition(cid, state, reason="synthetic conflicting state")
        before = tuple(runtime.oms.db.iterdump())
        plan = recovery_for(runner).inspect(cid, tenant_id="pilot")
        assert plan.status == "BLOCKED" and plan.reason == "paper_recovery_order_state_requires_review"
        assert tuple(runtime.oms.db.iterdump()) == before
    finally:
        close(runner)


def test_already_matching_order_is_inspection_only(tmp_path, monkeypatch):
    monkeypatch.setenv("PRAMANA_RELEASE_REVISION", "a" * 40)
    runner = build(tmp_path)
    try:
        assert submit_bound(runner).fill is not None
        runtime, cid = identifiers(runner)
        before = tuple(runtime.oms.db.iterdump())
        assert recovery_for(runner).inspect(cid, tenant_id="pilot").status == "MATCHED"
        assert tuple(runtime.oms.db.iterdump()) == before
        assert not runtime.oms.db.execute("SELECT 1 FROM sqlite_master WHERE name='oms_paper_recoveries'").fetchone()
    finally:
        close(runner)


def test_outer_transactions_are_refused_without_committing_callers_work(tmp_path, monkeypatch):
    runner = interrupted_runner(tmp_path, monkeypatch)
    try:
        runtime, cid = identifiers(runner)
        with runtime.oms.transaction():
            with pytest.raises(ValueError, match="outer_transaction"):
                recovery_for(runner).inspect(cid, tenant_id="pilot")
            assert runtime.oms.db.in_transaction
    finally:
        close(runner)


def test_missing_recovery_audit_cannot_look_like_ordinary_completed_fill(tmp_path, monkeypatch):
    runner = interrupted_runner(tmp_path, monkeypatch)
    try:
        runtime, cid = identifiers(runner)
        recovery = recovery_for(runner)
        plan = recovery.inspect(cid, tenant_id="pilot")
        recovery.recover(cid, tenant_id="pilot", reviewed_plan_sha256=plan.sha256, reviewer="synthetic-reviewer")
        with runtime.oms.db:
            runtime.oms.db.execute("DROP TABLE oms_paper_recoveries")
        assert recovery.inspect(cid, tenant_id="pilot").status == "BLOCKED"
        with pytest.raises(ValueError, match="oms_paper_recovery_audit_missing"):
            runtime.oms.verify(cid)
    finally:
        close(runner)


def test_committed_sell_recovery_does_not_recredit_cash_or_recreate_position(tmp_path, monkeypatch):
    from decimal import Decimal as D

    from test_pilot_closure import INSTRUMENT
    from test_runtime_contract_mode import NOW

    from quant_ai.agents.contracts import Stance
    from quant_ai.agents.swarm import AtlasCIOAgent
    from quant_ai.domain.models import PortfolioSnapshot
    monkeypatch.setenv("PRAMANA_RELEASE_REVISION", "a" * 40)
    runner = build(tmp_path)
    try:
        assert submit_bound(runner, "buy-before-sale").fill is not None
        d = runner.daemon
        pipeline = d.scheduler.pipeline
        runtime = pipeline.runtime
        analysis = pipeline._analysis_request(INSTRUMENT, NOW, {}, 0)
        decision = SimpleNamespace(action=Stance.SELL, cycle_id="interrupted-sale", confidence=D('.8'),
            expected_return=D('.02'), expected_risk=D('.01'), rationale=("synthetic",), provenance=None)
        proposal = AtlasCIOAgent._proposal_from_decision(analysis, decision, quantity=1,
            reference_price=D(100), stop_price=D(105), take_profit_price=D(90), country="INDIA")
        original = runtime.broker.submit_with_evidence
        def fail_after_sale(*args):
            original(*args)
            raise ValueError("synthetic_error_after_sale")
        with monkeypatch.context() as patch:
            patch.setattr(runtime.broker, "submit_with_evidence", fail_after_sale)
            result = runtime._execute_proposal(analysis, (), proposal, d.plan,
                PortfolioSnapshot(D(100000), D(0), D(0)), None, "pilot")
        assert result.order_state is OrderState.SUBMISSION_UNCERTAIN
        assert not runtime.broker.get_positions("pilot")
        assert d._pilot_pre_submit(SimpleNamespace(side=Side.BUY)) == "runtime_identity_oms_recovery_required"
        cid = runtime.oms.open_orders("pilot")[0].client_order_id
        before = tuple(runtime.broker._connection.iterdump())
        recovery = recovery_for(runner)
        plan = recovery.inspect(cid, tenant_id="pilot")
        assert plan.status == "READY"
        assert recovery.recover(cid, tenant_id="pilot", reviewed_plan_sha256=plan.sha256, reviewer="synthetic-reviewer").status == "MATCHED"
        assert tuple(runtime.broker._connection.iterdump()) == before
        assert len(runtime.broker.ledger_entries("pilot")) == 2
    finally:
        close(runner)


def test_recovery_marks_schema_compatibility_only_on_success(tmp_path, monkeypatch):
    from quant_ai.orders.oms import DurableOms
    runner = interrupted_runner(tmp_path, monkeypatch)
    try:
        runtime, cid = identifiers(runner)
        assert runtime.oms.db.execute("SELECT version FROM oms_meta").fetchone()[0] == 1
        recovery = recovery_for(runner)
        plan = recovery.inspect(cid, tenant_id="pilot")
        assert runtime.oms.db.execute("SELECT version FROM oms_meta").fetchone()[0] == 1
        recovery.recover(cid, tenant_id="pilot", reviewed_plan_sha256=plan.sha256, reviewer="synthetic-reviewer")
        assert runtime.oms.db.execute("SELECT version FROM oms_meta").fetchone()[0] == 2
        with DurableOms(tmp_path / "oms.sqlite") as reopened:
            assert reopened.verify(cid)["verified"]
        with runtime.oms.db:
            runtime.oms.db.execute("UPDATE oms_meta SET version=1")
        with pytest.raises(ValueError, match="recovery_schema_mismatch"):
            runtime.oms.verify(cid)
    finally:
        close(runner)


@pytest.mark.parametrize("operation", ["inspect", "recover"])
def test_recovery_audit_cannot_be_replayed_before_it_was_recorded(tmp_path, monkeypatch, operation):
    from datetime import datetime, timezone

    runner = interrupted_runner(tmp_path, monkeypatch)
    try:
        runtime, cid = identifiers(runner)
        service = recovery_for(runner)
        moment = datetime.now(timezone.utc) + timedelta(minutes=1)
        plan = service.inspect(cid, tenant_id="pilot", now=moment)
        service.recover(cid, tenant_id="pilot", reviewed_plan_sha256=plan.sha256,
                        reviewer="synthetic-reviewer", now=moment)
        before = tuple(runtime.oms.db.iterdump())
        earlier = moment - timedelta(seconds=1)
        if operation == "inspect":
            observed = service.inspect(cid, tenant_id="pilot", now=earlier)
            assert observed.status == "BLOCKED"
            assert observed.reason == "paper_recovery_audit_from_future"
        else:
            with pytest.raises(ValueError, match="paper_recovery_audit_from_future"):
                service.recover(cid, tenant_id="pilot", reviewed_plan_sha256=plan.sha256,
                                reviewer="synthetic-reviewer", now=earlier)
        assert tuple(runtime.oms.db.iterdump()) == before
        assert runner.daemon.kill_switch.engaged
    finally:
        close(runner)


@pytest.mark.parametrize("operation", ["inspect", "recover"])
def test_implicit_recovery_clock_is_sampled_after_database_serialization(tmp_path, monkeypatch, operation):
    from contextlib import contextmanager
    from datetime import datetime, timezone

    from quant_ai.orders import paper_recovery

    runner = interrupted_runner(tmp_path, monkeypatch)
    try:
        runtime, cid = identifiers(runner)
        service = recovery_for(runner)
        recorded_at = datetime.now(timezone.utc) + timedelta(minutes=1)
        plan = service.inspect(cid, tenant_id="pilot", now=recorded_at)
        service.recover(cid, tenant_id="pilot", reviewed_plan_sha256=plan.sha256,
                        reviewer="synthetic-reviewer", now=recorded_at)
        before = tuple(runtime.oms.db.iterdump())
        clock = {"now": recorded_at - timedelta(seconds=1)}
        real_now, transaction = paper_recovery._now, runtime.oms.transaction
        @contextmanager
        def acquired_transaction():
            with transaction():
                clock["now"] = recorded_at + timedelta(seconds=1)
                yield
        with monkeypatch.context() as patch:
            patch.setattr(paper_recovery, "_now", lambda value: real_now(clock["now"] if value is None else value))
            patch.setattr(runtime.oms, "transaction", acquired_transaction)
            if operation == "inspect":
                result = service.inspect(cid, tenant_id="pilot")
            else:
                result = service.recover(cid, tenant_id="pilot", reviewed_plan_sha256=plan.sha256,
                                         reviewer="synthetic-reviewer")
        assert result.status == "MATCHED"
        assert tuple(runtime.oms.db.iterdump()) == before
    finally:
        close(runner)
