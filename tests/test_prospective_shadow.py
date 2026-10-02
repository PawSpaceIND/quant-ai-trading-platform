"""Hermetic passive recording and actual daemon-hook regressions."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from threading import Event
from types import SimpleNamespace

import pytest
from test_pilot_closure import runner_for
from test_shadow_forecast_lineage import NOW, bundle

from quant_ai.agents.feature_snapshot import freeze
from quant_ai.learning import prospective, shadow
from quant_ai.marketdata.ticker_stream import LiveTick, TickBuffer


def candidate_plan():
    original = bundle(feature_names=['market_last_price'],coefficients=['0.01'])
    model = shadow.decode(original.artifact)
    model.update(schema=shadow.MODEL_SCHEMA_V2,endpoint_policy_id=shadow.ENDPOINT_POLICY,
                 maximum_endpoint_age_seconds=60,cost_policy_id='fixed_shadow_round_trip.v1',
                 cost_policy_sha256=shadow._hash({'id':'fixed_shadow_round_trip.v1','cost_return':'0.001'}))
    raw = json.dumps(model).encode()
    dataset = replace(original.dataset,source_ids=('zerodha',),cost_policy_id=model['cost_policy_id'],
                      label_schema_sha256=shadow.label_schema_digest(model))
    from quant_ai.learning.training import training_dataset_digest
    run = replace(original.training_run,artifact_sha256=shadow._sha(raw),
                  dataset_manifest_sha256=training_dataset_digest(dataset))
    candidate = shadow.ShadowModelBundle(dataset,run,raw)
    return {'schema':prospective.SCHEMA,'frozen_at':(NOW-timedelta(days=1)).isoformat(),
            'bundle':candidate.payload(),'cost_return':'0.001'}


def observer(tmp_path, clock=None):
    value = candidate_plan()
    buffer = TickBuffer(clock=lambda: NOW)
    instance = prospective.ProspectiveShadowObserver(shadow._canonical(value).encode(),
        reviewed_sha256=shadow._hash(value),journal_path=tmp_path/'shadow.sqlite',
        tenant_id='pilot',buffer=buffer,paper_only=True,clock=clock or (lambda: NOW))
    return instance


def capture(pair='decision-one', **changes):
    tick = LiveTick('INFY',Decimal(100),Decimal(10),Decimal(99),Decimal(101),NOW,'zerodha')
    value = {'decision_id':pair,'proof_decision_id':'proof-'+pair,'snapshot':freeze(subject='INFY',frozen_at=NOW,
                                               metrics={},evidence=(),market_tick=tick)}
    value['snapshot'].update(changes)
    return value


@pytest.mark.parametrize('restart',[False,True])
@pytest.mark.parametrize('interrupted',[False,True])
def test_pending_resolution_is_bounded_fair_and_survives_restart(tmp_path,restart,interrupted,monkeypatch):
    current=[NOW]
    item=observer(tmp_path,lambda:current[0])
    requests=[capture('pending-'+str(i),subject='UNKNOWN'+str(i)) for i in range(64)]
    requests.append(capture('qualified'))
    try:
        item.process(requests[:50])
        assert item.process(requests[50:])['pending']==65
        current[0]=NOW+timedelta(seconds=60)
        # The first bounded page contains only unknown endpoints.
        if interrupted:
            with monkeypatch.context() as patch:
                def fail(*args):
                    raise RuntimeError('interrupted endpoint lookup')
                patch.setattr(item.buffer,'latest_at_or_before',fail)
                with pytest.raises(RuntimeError,match='interrupted endpoint lookup'):
                    item.process([])
        else:
            assert item.process([])['resolved']==0
        if restart:
            item.close()
            item=observer(tmp_path,lambda:current[0])
        item.buffer.clock=lambda:current[0]
        assert item.buffer.put(LiveTick('INFY',Decimal(101),Decimal(10),None,None,current[0],'zerodha'))
        report=item.process([])
        assert report['resolved']==1 and report['pending']==64
        # Wrapping preserves unknowns and never duplicates a completed outcome.
        assert item.process([])['resolved']==1
        assert item.process([])['pending']==64
    finally:
        item.close()


def test_default_off_never_reads_plan_creates_store_or_constructs_worker(monkeypatch):
    monkeypatch.setattr(prospective,'ProspectiveShadowObserver',lambda *a,**k:pytest.fail('off constructor'))
    assert prospective.observer_from_env(buffer=None,tenant_id='pilot',paper_only=False,
        environ={'PRAMANA_PROSPECTIVE_SHADOW_PLAN':'absent'}) is None


def test_record_resolve_score_cost_and_restart_idempotence(tmp_path):
    current = [NOW]
    item = observer(tmp_path,lambda:current[0])
    try:
        first = item.process([capture()])
        assert first['pending']==1 and first['resolved']==0 and first['candidate'] is None
        assert item.process([capture()])['capture_status_counts']=={'recorded':1}
        current[0]=NOW+timedelta(seconds=60)
        item.buffer.clock=lambda:current[0]
        assert item.buffer.put(LiveTick('INFY',Decimal('100.05'),Decimal(10),None,None,current[0],'zerodha'))
        report=item.process([])
        assert report['pending']==0 and report['resolved']==1
        assert report['candidate']['positive_rate']=='0'
        assert report['constant_half_baseline']['brier_score']=='0.25'
        assert report['calibration_verified'] is False and report['trading_authorized'] is False
        assert item.process([])==report
    finally:
        item.close()
    restarted=observer(tmp_path,lambda:current[0])
    try:
        assert restarted.process([capture()])['resolved']==1
    finally:
        restarted.close()


@pytest.mark.parametrize('change',[
    {'frozen_at':(NOW+timedelta(seconds=1)).isoformat()},
    {'frozen_at':(NOW-timedelta(days=2)).isoformat()},
    {'metrics_truncated':True}, {'market':{}},
    {'market':{'source':'zerodha','observed_at':NOW.isoformat(),'last_price':None}},
    {'market':{'source':'zerodha','observed_at':NOW.isoformat(),'last_price':'NaN'}},
])
def test_refused_inputs_stay_in_denominator_without_prediction(tmp_path,change):
    item=observer(tmp_path)
    try:
        report=item.process([capture(**change)])
        assert report['capture_status_counts']=={'refused':1}
        assert report['pending']==report['resolved']==0 and report['candidate'] is None
    finally:
        item.close()


def test_unknown_or_late_endpoint_stays_pending(tmp_path):
    current=[NOW]
    item=observer(tmp_path,lambda:current[0])
    try:
        item.process([capture()])
        current[0]=NOW+timedelta(seconds=61)
        item.buffer.clock=lambda:current[0]
        item.buffer.put(LiveTick('INFY',Decimal(90),Decimal(10),None,None,current[0],'zerodha'))
        assert item.process([])['pending']==1
        assert item.process([])['resolved']==0
    finally:
        item.close()


def test_changed_plan_or_reused_decision_refuses_without_history_rewrite(tmp_path):
    item=observer(tmp_path)
    try:
        item.process([capture()])
        with pytest.raises(ValueError,match='decision_input_reused'):
            item.process([capture(frozen_at=(NOW-timedelta(seconds=1)).isoformat())])
        item.plan['cost_return']='0.002'
        with pytest.raises(ValueError,match='frozen_plan_changed'):
            item.process([])
    finally:
        item.close()


def test_worker_bounded_nonblocking_copies_inputs_and_can_close(tmp_path,monkeypatch):
    item=observer(tmp_path)
    entered,release=Event(),Event()
    seen=[]
    def slow(requests):
        entered.set()
        assert release.wait(3)
        seen.extend(requests)
    monkeypatch.setattr(item,'process',slow)
    request=capture()
    try:
        assert item.submit([request])=='submitted' and entered.wait(1)
        request['snapshot']['subject']='MUTATED'
        async def heartbeat():
            await asyncio.sleep(0)
            return True
        assert asyncio.run(heartbeat())
        assert item.submit([capture('second')])=='worker_busy'
        assert item.status()['dropped_capture_count_since_start']==1
    finally:
        release.set()
        item._future.result(timeout=3)
        item.close()
    assert seen[0]['snapshot']['subject']=='INFY' and item.submit([])=='closed'


def test_actual_daemon_hook_records_same_decision_and_isolates_observer_failure(tmp_path,monkeypatch):
    runner=runner_for(tmp_path)
    daemon=runner.daemon
    requests=[]
    class Passive:
        def submit(self, rows):
            requests.extend(rows)
            raise OSError('synthetic failure')
    daemon.prospective_shadow=Passive()
    proposal=SimpleNamespace(symbol='INFY',decision_id='actual-proof-id',provenance={'feature_snapshot':capture()['snapshot']})
    original_tick=daemon.scheduler.run_tick_async
    async def tick(*a,**k):
        brief=await original_tick(*a,**k)
        daemon.scheduler.last_result=SimpleNamespace(execution=SimpleNamespace(proposal=proposal,trace=SimpleNamespace(decision_id='distinct-proof-id')))
        return brief
    # Force async route; substitute only analysis result, preserving daemon integration.
    daemon.scheduler.pipeline.runtime.cio.atlas.llm_client=object()
    monkeypatch.setattr(daemon.scheduler,'run_tick_async',tick)
    monkeypatch.setattr(daemon,'_journal_decision',lambda *a,**k:None)
    monkeypatch.setattr(daemon,'_primary_brief',lambda rows:rows[0])
    monkeypatch.setattr(daemon,'_resolve_decision_outcomes',lambda *a:None)
    try:
        asyncio.run(daemon.run_once(NOW))
        assert requests[0]['decision_id']=='actual-proof-id'
        assert requests[0]['proof_decision_id']=='distinct-proof-id'
        assert requests[0]['snapshot']==capture()['snapshot']
    finally:
        runner.daemon.tracker.broker.close()


def test_missing_proof_or_decision_ids_are_refused_not_substituted(tmp_path):
    item=observer(tmp_path)
    missing_proof=capture()
    missing_proof['proof_decision_id']=None
    missing_decision=capture('another')
    missing_decision['decision_id']=None
    try:
        report=item.process([missing_proof,missing_decision])
        assert report['capture_status_counts']=={'refused':1}
        assert report['unlinked_refused_inputs']==1 and report['pending']==0
    finally:
        item.close()


def test_same_opportunity_with_new_id_does_not_inflate_forecast_samples(tmp_path):
    item=observer(tmp_path)
    try:
        report=item.process([capture('one'),capture('two')])
        assert report['capture_status_counts']=={'recorded':1,'refused':1}
        assert report['pending']==1
    finally:
        item.close()


def test_crash_after_committed_forecast_receipt_before_ack_is_idempotent(tmp_path,monkeypatch):
    current=[NOW]
    item=observer(tmp_path,lambda:current[0])
    original=shadow.ShadowForecastWriter.record
    def interrupted(writer,*args,**kwargs):
        original(writer,*args,**kwargs)
        raise RuntimeError('synthetic committed forecast')
    monkeypatch.setattr(shadow.ShadowForecastWriter,'record',interrupted)
    try:
        with pytest.raises(RuntimeError):
            item.process([capture()])
        current[0]=NOW+timedelta(days=1)
        monkeypatch.setattr(shadow.ShadowForecastWriter,'record',original)
        report=item.process([capture()])
        assert report['pending']==1 and report['unlinked_forecasts']==0
        with shadow.ShadowForecastWriter(item.path,tenant_id='pilot') as writer:
            row=writer.journal.db.execute('SELECT decision_at FROM probability_forecasts').fetchone()
            assert row[0]==NOW.isoformat()
    finally:
        item.close()


def test_crash_after_endpoint_evidence_keeps_exact_witness_for_restart(tmp_path,monkeypatch):
    from quant_ai.learning.outcomes import ForecastOutcomeJournal
    current=[NOW]
    item=observer(tmp_path,lambda:current[0])
    original=ForecastOutcomeJournal.resolve
    def interrupted(*a,**k):
        raise RuntimeError('synthetic before settlement')
    try:
        item.process([capture()])
        current[0]=NOW+timedelta(seconds=60)
        item.buffer.clock=lambda:current[0]
        item.buffer.put(LiveTick('INFY',Decimal(101),Decimal(10),None,None,current[0],'zerodha'))
        monkeypatch.setattr(ForecastOutcomeJournal,'resolve',interrupted)
        with pytest.raises(RuntimeError):
            item.process([])
        monkeypatch.setattr(ForecastOutcomeJournal,'resolve',original)
        current[0]+=timedelta(days=1)
        item.buffer=TickBuffer()  # original tick no longer available
        report=item.process([])
        assert report['resolved']==1 and report['pending']==0
    finally:
        item.close()


@pytest.mark.parametrize('gate',[
    {'PRAMANA_PROSPECTIVE_SHADOW_ENABLED':'true','TRADING_LIVE_MONEY_ACTIVE':'true'},
    {'PRAMANA_PROSPECTIVE_SHADOW_ENABLED':'TRUE'},
])
def test_unknown_or_live_activation_refuses_without_opening_store(gate):
    with pytest.raises(ValueError,match='passive_paper_gate'):
        prospective.observer_from_env(buffer=TickBuffer(),tenant_id='pilot',paper_only=True,environ=gate)


def test_modified_review_digest_refuses_before_worker_or_store(tmp_path):
    value=candidate_plan()
    with pytest.raises(ValueError,match='reviewed_plan_digest'):
        prospective.ProspectiveShadowObserver(shadow._canonical(value).encode(),
            reviewed_sha256='a'*64,journal_path=tmp_path/'absent.sqlite',tenant_id='pilot',
            buffer=TickBuffer(),paper_only=True)
    assert not (tmp_path/'absent.sqlite').exists()


def test_forecast_and_capture_receipt_roll_back_together(tmp_path,monkeypatch):
    item=observer(tmp_path)
    original=item._persist_capture
    def interrupted(db,*values):
        original(db,*values)
        raise RuntimeError('synthetic before commit')
    monkeypatch.setattr(item,'_persist_capture',interrupted)
    try:
        with pytest.raises(RuntimeError):
            item.process([capture()])
        with shadow.ShadowForecastWriter(item.path,tenant_id='pilot') as writer:
            assert writer.journal.db.execute('SELECT COUNT(*) FROM probability_forecasts').fetchone()[0]==0
            assert writer.journal.db.execute('SELECT COUNT(*) FROM prospective_captures').fetchone()[0]==0
        monkeypatch.setattr(item,'_persist_capture',original)
        assert item.process([capture()])['pending']==1
    finally:
        item.close()


@pytest.mark.parametrize('endpoint_seconds,resolved',[(50,True),(-1,False),(61,False)])
def test_versioned_horizon_uses_fresh_post_decision_cutoff_tick_only(tmp_path,endpoint_seconds,resolved):
    current=[NOW]
    item=observer(tmp_path,lambda:current[0])
    try:
        item.process([capture()])
        current[0]=NOW+timedelta(seconds=61)
        item.buffer.clock=lambda:current[0]
        item.buffer.put(LiveTick('INFY',Decimal(101),Decimal(10),None,None,
            NOW+timedelta(seconds=endpoint_seconds),'zerodha'))
        report=item.process([])
        assert report['resolved']==int(resolved) and report['pending']==int(not resolved)
    finally:
        item.close()


def test_v1_label_digest_stays_pinned_and_cannot_be_silently_adopted(tmp_path):
    original=bundle()
    model=original.validate()
    assert shadow.label_schema_digest(model)==shadow._hash({'event':shadow.EVENT,
        'horizon_seconds':model['horizon_seconds'],'cost_policy_sha256':model['cost_policy_sha256']})
    value=candidate_plan()
    value['bundle']=original.payload()
    with pytest.raises(ValueError,match='versioned_endpoint_policy_required'):
        prospective.ProspectiveShadowObserver(shadow._canonical(value).encode(),
            reviewed_sha256=shadow._hash(value),journal_path=tmp_path/'absent.sqlite',
            tenant_id='pilot',buffer=TickBuffer(),paper_only=True)
    assert not (tmp_path/'absent.sqlite').exists()


def test_background_disk_failure_is_observable_without_a_second_submission(tmp_path,monkeypatch):
    item=observer(tmp_path)
    def fail(*args):
        raise OSError('synthetic disk failure')
    monkeypatch.setattr(item,'process',fail)
    finished=Event()
    original_complete=item._complete
    def complete(future):
        original_complete(future)
        finished.set()
    monkeypatch.setattr(item,'_complete',complete)
    try:
        item.submit([capture()])
        with pytest.raises(OSError):
            item._future.result(timeout=2)
        # Future completion callback updates evidence-only status; no retry/cap/order action.
        assert finished.wait(2)
        assert item.status()['failed_batches_since_start']==1
    finally:
        item.close()


def test_closed_daemon_has_no_shadow_forecasts_and_retains_protection(tmp_path,monkeypatch):
    runner=runner_for(tmp_path)
    item=observer(tmp_path)
    runner.daemon.prospective_shadow=item
    try:
        asyncio.run(runner.daemon.run_once(NOW))  # NSE 15:30, regular session closed
        item._future.result(timeout=2)
        assert item.last_report['capture_status_counts']=={}
        assert item.last_report['pending']==item.last_report['resolved']==0
        assert runner.daemon.tracker.broker.ledger_entries('pilot')==()
        runner.daemon.request_stop()
        assert item.submit([])=='closed'
    finally:
        item.close()
        runner.daemon.tracker.broker.close()


def test_read_evaluation_never_constructs_writer_or_changes_journal(tmp_path,monkeypatch):
    import hashlib
    item=observer(tmp_path)
    try:
        expected=item.process([capture()])
        before=hashlib.sha256(item.path.read_bytes()).hexdigest()
        monkeypatch.setattr(prospective,'ShadowForecastWriter',lambda *a,**k:pytest.fail('read writer'))
        result=prospective.read_evaluation(item.path,raw_plan=shadow._canonical(item.plan).encode(),
            reviewed_sha256=item.plan_sha,tenant_id='pilot',now=NOW)
        assert result==expected
        assert hashlib.sha256(item.path.read_bytes()).hexdigest()==before
        with pytest.raises(ValueError,match='journal_tenant_binding'):
            prospective.read_evaluation(item.path,raw_plan=shadow._canonical(item.plan).encode(),
                reviewed_sha256=item.plan_sha,tenant_id='other',now=NOW)
    finally:
        item.close()


def test_exchange_event_time_is_not_backdated_input_availability(tmp_path):
    later=NOW+timedelta(seconds=10)
    item=observer(tmp_path,lambda:later)
    try:
        assert item.process([capture()])['pending']==1
        with shadow.ShadowForecastWriter(item.path,tenant_id='pilot') as writer:
            payload=json.loads(writer.journal.db.execute('SELECT payload FROM shadow_forecast_inputs').fetchone()[0])
            assert payload['input']['observed_at']==NOW.isoformat()
            assert payload['input']['available_at']==later.isoformat()
            assert payload['decision_at']==later.isoformat()
    finally:
        item.close()
