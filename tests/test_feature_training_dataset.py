from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Context, Decimal, localcontext

import pytest

from quant_ai.features.store import FeatureObservation, PointInTimeFeatureStore
from quant_ai.learning import feature_dataset as fd
from quant_ai.learning.contracts import AccessPlane, KnowledgeCategory, RightsStatus, SourceGrant

D = Decimal
T = datetime(2026, 1, 2, 10, tzinfo=timezone.utc)


def grants():
    return (SourceGrant('recorded', 'synthetic-test', frozenset({KnowledgeCategory.MARKET,
            KnowledgeCategory.BROKER}), frozenset({AccessPlane.TRAINING}), True, RightsStatus.INTERNAL),)


def plan(count=1):
    def rule(feature, schema, category):
        return {'feature': feature, 'source_id': 'recorded', 'schema_id': schema, 'max_age_seconds': 60, 'category': category}
    return {'schema': fd.PLAN_SCHEMA, 'partition': 'training', 'dataset_id': 'synthetic-data', 'cutoff': (T + timedelta(hours=4)).isoformat(), 'horizon_seconds': 60, 'maximum_feature_age_seconds': 60, 'cost_policy_id': 'supplied-test-cost', 'cost_policy_sha256': 'c'*64, 'adjustment_policy_id': 'unadjusted-test', 'features': [rule('signal', 'feature:v1', 'MARKET')], 'price': rule('price', 'price:unadjusted-test', 'MARKET'), 'cost': rule('cost', 'cost_fraction:'+'c'*64, 'BROKER'), 'decisions': [{'row_id': f'r{i}', 'subject': 'INDIA:NSE:TEST:INR', 'decision_at': (T + timedelta(minutes=2*i)).isoformat()} for i in range(count)]}


def populate(path, count=1):
    p = plan(count)
    with PointInTimeFeatureStore(path) as store:
        for i, decision in enumerate(p['decisions']):
            at = datetime.fromisoformat(decision['decision_at'])
            subject = decision['subject']
            for rule, value, observed, available, suffix in (
                (p['features'][0], '1' if i % 2 == 0 else '-1', at, at, 'f'),
                (p['price'], '100', at, at, 'entry'),
                (p['cost'], '0.001', at, at, 'cost'),
                (p['price'], '102' if i % 2 == 0 else '99', at+timedelta(seconds=60),
                 at+timedelta(seconds=62), 'exit'),
            ):
                store.append(FeatureObservation(f'{i}-{suffix}', subject, rule['feature'], D(value),
                    observed, available, rule['source_id'], rule['schema_id']))
    return p


def build(path, p=None, selected=None):
    return fd.build_training_package(path, fd.canonical(p or plan()),
        source_grants=grants() if selected is None else selected, now=T+timedelta(days=1))


def inspect_package(raw):
    return json.loads(raw)


def rehash(payload):
    payload['sha256'] = hashlib.sha256(fd.canonical({k:v for k,v in payload.items()
                                                   if k != 'sha256'})).hexdigest()
    return fd.canonical(payload)


def rewrite(path, sql, args=()):
    with sqlite3.connect(path) as db:
        db.execute('DROP TRIGGER IF EXISTS feature_observations_update_blocked')
        db.execute('DROP TRIGGER feature_observations_delete_blocked')
        db.execute(sql, args)


def test_feature_store_has_a_training_dataset_bridge():
    assert importlib.util.find_spec('quant_ai.learning.feature_dataset') is not None


def test_build_uses_recorded_prices_costs_and_exact_original_features(tmp_path):
    path=tmp_path/'features.db'; p=populate(path, 2)
    raw=build(path,p); package=inspect_package(raw)
    first, second=package['data']['rows']
    assert first['values'] == {'signal':'1'}
    assert D(first['gross_return']) == D('0.02')
    assert D(second['gross_return']) == D('-0.01')
    assert D(first['cost_fraction']) == D('0.001')
    assert first['outcome_available_at'] == (T+timedelta(seconds=62)).isoformat()
    assert len(package['observations']) == 8
    data=fd.validate_training_package(raw,source_grants=grants(),now=T+timedelta(days=1))
    assert json.loads(data) == package['data']
    assert raw == build(path,p)


def test_negative_net_move_is_not_labelled_a_success(tmp_path):
    path=tmp_path/'f.db'; populate(path)
    with PointInTimeFeatureStore(path) as store:
        p=plan(); at=T
        # Latest available revision at the SAME decision, retaining full lineage.
        store.append(FeatureObservation('new-cost','INDIA:NSE:TEST:INR','cost',D('.03'),
            at,at+timedelta(microseconds=1),'recorded',p['cost']['schema_id']))
    # That revision was unavailable at decision and cannot affect its label.
    row=inspect_package(build(path))['data']['rows'][0]
    assert D(row['cost_fraction'])==D('.001')


def test_features_use_the_original_known_revision_not_later_history(tmp_path):
    path=tmp_path/'f.db'; populate(path)
    with PointInTimeFeatureStore(path) as store:
        store.append(FeatureObservation('revision','INDIA:NSE:TEST:INR','signal',D('999'),T,
                                        T+timedelta(seconds=1),'recorded','feature:v1'))
    row=inspect_package(build(path))['data']['rows'][0]
    assert row['values']=={'signal':'1'}


def test_endpoint_uses_first_known_revision_within_cutoff(tmp_path):
    path=tmp_path/'f.db'; populate(path)
    with PointInTimeFeatureStore(path) as store:
        store.append(FeatureObservation('revision','INDIA:NSE:TEST:INR','price',D('500'),
            T+timedelta(seconds=60),T+timedelta(seconds=65),'recorded','price:unadjusted-test'))
    package=inspect_package(build(path))
    assert D(package['data']['rows'][0]['gross_return'])==D('.02')
    assert not any(o['observationId']=='revision' for o in package['observations'])


@pytest.mark.parametrize('fault',['missing','later','not_yet_known','wrong_source','wrong_schema'])
def test_exact_endpoint_is_required_not_nearby_or_incompatible_quotes(tmp_path,fault):
    path=tmp_path/'f.db'; populate(path)
    updates={'missing':('DELETE FROM feature_observations WHERE observation_id=?',('0-exit',)),
        'later':('UPDATE feature_observations SET observed_at=? WHERE observation_id=?',((T+timedelta(seconds=61)).isoformat(),'0-exit')),
        'not_yet_known':('UPDATE feature_observations SET available_at=? WHERE observation_id=?',((T+timedelta(days=3)).isoformat(),'0-exit')),
        'wrong_source':('UPDATE feature_observations SET source_id=? WHERE observation_id=?',('other','0-exit')),
        'wrong_schema':('UPDATE feature_observations SET schema_id=? WHERE observation_id=?',('other','0-exit'))}
    rewrite(path,*updates[fault])
    with pytest.raises(ValueError,match='exact_endpoint_missing'):
        build(path)


def test_hash_mismatch_never_becomes_training_data(tmp_path):
    path=tmp_path/'f.db'; populate(path)
    rewrite(path,"UPDATE feature_observations SET value='777' WHERE observation_id='0-f'")
    with pytest.raises(ValueError,match='hash_mismatch'):
        build(path)


def test_missing_predictor_refuses_entire_fixed_selection(tmp_path):
    path=tmp_path/'f.db'; p=populate(path,2)
    rewrite(path,"DELETE FROM feature_observations WHERE observation_id='1-f'")
    with pytest.raises(ValueError,match='feature_stale'):
        build(path,p)


@pytest.mark.parametrize('key,value,reason',[
    ('partition','holdout','plan_schema'),('schema','other','plan_schema'),
    ('horizon_seconds',False,'time_bound'),('maximum_feature_age_seconds',0,'time_bound'),
    ('features',[],'feature_count'),('cost_policy_sha256','not-a-hash','cost_policy'),
    ('decisions',[],'decision_bound'),('cutoff',(T+timedelta(days=2)).isoformat(),'future_cutoff')])
def test_plan_refuses_invalid_selection_before_source_is_opened(tmp_path,key,value,reason):
    p=plan(); p[key]=value
    with pytest.raises(ValueError,match='feature_dataset_'+reason):
        build(tmp_path/'not-created.db',p)
    assert not (tmp_path/'not-created.db').exists()


@pytest.mark.parametrize('change,reason',[
    ('order','decision_order'),('overlap','duplicate_or_overlap'),('duplicate','duplicate_or_overlap'),
    ('future','outcome_not_due'),('names','feature_names'),('cost_binding','price_cost_binding'),
    ('price_binding','price_cost_binding'),('categories','price_cost_categories'),('collision','role_collision')])
def test_plan_identity_time_and_semantics_are_fixed_before_data(tmp_path,change,reason):
    p=plan(2)
    if change=='order': p['decisions'].reverse()
    if change=='overlap': p['decisions'][1]['decision_at']=(T+timedelta(seconds=30)).isoformat()
    if change=='duplicate': p['decisions'][1]['row_id']='r0'
    if change=='future': p['cutoff']=(T+timedelta(seconds=59)).isoformat()
    if change=='names': p['features'].append(copy.deepcopy(p['features'][0]))
    if change=='cost_binding': p['cost']['schema_id']='cost_fraction:'+'d'*64
    if change=='price_binding': p['price']['schema_id']='price:different'
    if change=='categories': p['cost']['category']='MARKET'
    if change=='collision': p['cost']['feature']='price'
    with pytest.raises(ValueError,match='feature_dataset_'+reason):
        build(tmp_path/'missing.db',p)


@pytest.mark.parametrize('change',['rights','plane','point_in_time','category','unknown','duplicate','type'])
def test_training_grants_must_cover_every_source(tmp_path,change):
    g=grants()[0]; gs=(g,)
    if change=='rights': gs=(replace(g,rights_status=RightsStatus.UNVERIFIED),)
    if change=='plane': gs=(replace(g,planes=frozenset({AccessPlane.RESEARCH})),)
    if change=='point_in_time': gs=(replace(g,point_in_time=False),)
    if change=='category': gs=(replace(g,categories=frozenset({KnowledgeCategory.MARKET})),)
    if change=='unknown': gs=(replace(g,source_id='other'),)
    if change=='duplicate': gs=(g,g)
    if change=='type': gs=({'source_id':'recorded'},)
    with pytest.raises(ValueError): build(tmp_path/'missing', selected=gs)


def test_source_cannot_be_created_or_modified_by_reader(tmp_path):
    path=tmp_path/'absent'
    with pytest.raises(FileNotFoundError): build(path)
    assert not path.exists()
    populate(path)
    before=path.read_bytes()
    with fd.ReadOnlyFeatureSource(path) as reader:
        with pytest.raises(sqlite3.OperationalError,match='readonly'):
            reader.db.execute('DELETE FROM feature_observations')
        with pytest.raises(ValueError,match='read_only'): reader.append(None)
    build(path)
    assert path.read_bytes()==before


@pytest.mark.parametrize('fault',['public','hardlink','symlink'])
def test_source_is_private_owned_unaliased_regular_file(tmp_path,fault):
    path=tmp_path/'f.db'; populate(path)
    if fault=='public': path.chmod(0o644)
    if fault=='hardlink': os.link(path,tmp_path/'alias')
    if fault=='symlink':
        other=tmp_path/'alias'; other.symlink_to(path); path=other
    with pytest.raises(ValueError,match='private_source'): build(path)


def test_single_read_snapshot_ignores_concurrent_later_revision(tmp_path,monkeypatch):
    path=tmp_path/'f.db'; populate(path)
    original=fd.ReadOnlyFeatureSource.snapshot
    changed=[]
    def snapshot(self,**kwargs):
        result=original(self,**kwargs)
        if not changed:
            with PointInTimeFeatureStore(path) as writer:
                writer.append(FeatureObservation('later','INDIA:NSE:TEST:INR','price',D('600'),
                    T+timedelta(seconds=60),T+timedelta(seconds=61),'recorded','price:unadjusted-test'))
            changed.append(True)
        return result
    monkeypatch.setattr(fd.ReadOnlyFeatureSource,'snapshot',snapshot)
    first=inspect_package(build(path)); second=inspect_package(build(path))
    assert D(first['data']['rows'][0]['gross_return'])==D('.02')
    assert D(second['data']['rows'][0]['gross_return'])==D('5')


def test_replaced_source_path_refuses_even_if_connection_is_still_valid(tmp_path,monkeypatch):
    path=tmp_path/'f.db'; populate(path)
    original=fd.ReadOnlyFeatureSource.snapshot
    def snapshot(self,**kwargs):
        result=original(self,**kwargs)
        path.rename(tmp_path/'old'); path.write_bytes(b'different'); path.chmod(0o600)
        return result
    monkeypatch.setattr(fd.ReadOnlyFeatureSource,'snapshot',snapshot)
    with pytest.raises(ValueError,match='source_replaced'): build(path)


@pytest.mark.parametrize('fault',['row_value','gross','cost','unused_observation','duplicate_observation','missing_observation','future_package'])
def test_rehashed_package_must_replay_its_actual_selected_records(tmp_path,fault):
    path=tmp_path/'f.db'; populate(path)
    payload=inspect_package(build(path))
    if fault=='row_value': payload['data']['rows'][0]['values']['signal']='999'
    if fault=='gross': payload['data']['rows'][0]['gross_return']='.9'
    if fault=='cost': payload['data']['rows'][0]['cost_fraction']='0'
    if fault=='unused_observation':
        row=copy.deepcopy(payload['observations'][0]); row['observationId']='unused'; row['subject']='another'
        payload['observations'].append(row)
    if fault=='duplicate_observation': payload['observations'].append(copy.deepcopy(payload['observations'][0]))
    if fault=='missing_observation': payload['observations'].pop()
    if fault=='future_package': payload['assembled_at']=(T+timedelta(days=2)).isoformat()
    with pytest.raises(ValueError):
        fd.validate_training_package(rehash(payload),source_grants=grants(),now=T+timedelta(days=1))


def test_package_hash_is_required(tmp_path):
    path=tmp_path/'f.db'; populate(path)
    payload=inspect_package(build(path)); payload['sha256']='d'*64
    with pytest.raises(ValueError,match='package_digest'):
        fd.validate_training_package(fd.canonical(payload),source_grants=grants(),now=T+timedelta(days=1))


def test_numeric_return_is_independent_of_callers_decimal_context(tmp_path):
    path=tmp_path/'f.db'; populate(path)
    expected=build(path)
    with localcontext(Context(prec=6)):
        assert build(path)==expected


def test_source_count_limit_is_enforced(tmp_path,monkeypatch):
    path=tmp_path/'f.db'; populate(path)
    monkeypatch.setattr(fd,'MAX_SOURCE_ROWS',3)
    with pytest.raises(ValueError,match='source_row_bound'): build(path)


def test_package_and_dataset_size_limits(tmp_path,monkeypatch):
    path=tmp_path/'f.db'; populate(path)
    monkeypatch.setattr(fd,'MAX_PACKAGE_BYTES',10)
    with pytest.raises(ValueError,match='package_size'): build(path)
    monkeypatch.setattr(fd,'MAX_PACKAGE_BYTES',12_000_000)
    monkeypatch.setattr(fd,'MAX_DATA_BYTES',10)
    with pytest.raises(ValueError,match='data_size'): build(path)


def test_duplicate_json_keys_and_extra_fields_are_refused(tmp_path):
    with pytest.raises(ValueError,match='duplicate_json_key'):
        fd.build_training_package(tmp_path/'missing',b'{"schema":1,"schema":2}',source_grants=grants(),now=T)
    p=plan(); p['holdout']=[]
    with pytest.raises(ValueError,match='plan_schema'): build(tmp_path/'missing',p)


def test_naive_clock_is_refused_before_any_source_work(tmp_path):
    with pytest.raises(ValueError,match='aware_time'):
        fd.build_training_package(tmp_path/'missing',fd.canonical(plan()),source_grants=grants(),now=T.replace(tzinfo=None))


def test_direct_boundary_helpers():
    with pytest.raises(ValueError,match='input_bound'): fd.decode(b'',10)
    with pytest.raises(ValueError,match='input_bound'): fd.decode(b'"long"',2)
    with pytest.raises(ValueError,match='nonfinite_json'): fd.decode(b'NaN',10)
    with pytest.raises(ValueError,match='identity'): fd._id('has whitespace')
    with pytest.raises(ValueError,match='numeric_text'): fd._number(3)
    with pytest.raises(ValueError,match='numeric_bound'): fd._number('1e-25')
    with pytest.raises(ValueError,match='numeric_bound'): fd._number('1e13')
    with pytest.raises(ValueError,match='numeric_bound'): fd._number('NaN')


def test_rule_schema_age_and_unused_grant_refuse_before_source(tmp_path):
    p=plan(); p['features'][0]['extra']=True
    with pytest.raises(ValueError,match='rule_schema'): build(tmp_path/'missing',p)
    p=plan(); p['features'][0]['max_age_seconds']=61
    with pytest.raises(ValueError,match='rule_age'): build(tmp_path/'missing',p)
    with pytest.raises(ValueError,match='grant_scope'):
        build(tmp_path/'missing', selected=(*grants(),replace(grants()[0],source_id='unused')))


def test_bad_decision_contract_refuses_before_source(tmp_path):
    p=plan(); p['decisions'][0]['confidence']='.9'
    with pytest.raises(ValueError,match='decision_schema'): build(tmp_path/'missing',p)


def test_point_schema_and_age_checked_after_selection(tmp_path):
    path=tmp_path/'f.db'; populate(path)
    from quant_ai.features.store import FeaturePoint
    p=plan(); rule=p['features'][0]
    point=FeaturePoint('signal',D('1'),T,T,'recorded','wrong','x')
    with pytest.raises(ValueError,match='point_binding'):
        fd._retain(point,'TEST',T,rule,{'recorded':grants()[0]}, {})
    old=T-timedelta(seconds=61)
    point=replace(point,observed_at=old,available_at=old,schema_id='feature:v1')
    with pytest.raises(ValueError,match='point_age'):
        fd._retain(point,'TEST',T,rule,{'recorded':grants()[0]}, {})
    point=replace(point,observed_at=T,available_at=T)
    with pytest.raises(ValueError,match='observation_reuse'):
        fd._retain(point,'TEST',T,rule,{'recorded':grants()[0]}, {'x':{'different':True}})


def replace_record(path,identifier,**changes):
    with sqlite3.connect(path) as db:
        db.row_factory=sqlite3.Row
        row=dict(db.execute('SELECT * FROM feature_observations WHERE observation_id=?',(identifier,)).fetchone())
        row.update(changes)
        row['value']=str(D(row['value']))
        item=FeatureObservation(row['observation_id'],row['subject'],row['feature'],D(row['value']),
            datetime.fromisoformat(row['observed_at']),datetime.fromisoformat(row['available_at']),
            row['source_id'],row['schema_id'])
        row['payload_sha256']=hashlib.sha256(fd.canonical(item.payload())).hexdigest()
        db.execute('DROP TRIGGER IF EXISTS feature_observations_update_blocked')
        db.execute('UPDATE feature_observations SET '+','.join(k+'=?' for k in row)+' WHERE observation_id=?',
                   (*row.values(),identifier))


@pytest.mark.parametrize('identifier,value,reason',[
    ('0-entry','0','price_cost_value'),('0-exit','-1','price_cost_value'),
    ('0-cost','-0.01','price_cost_value'),('0-exit','1200','return_bound')])
def test_prices_and_costs_must_be_usable(tmp_path,identifier,value,reason):
    path=tmp_path/'f.db'; populate(path); replace_record(path,identifier,value=value)
    with pytest.raises(ValueError,match=reason): build(path)


def test_rounding_cannot_flip_the_training_event(tmp_path):
    path=tmp_path/'f.db'; populate(path)
    # Positive change smaller than the output quantum cannot be turned into a negative label.
    replace_record(path,'0-entry',value='999999999999')
    replace_record(path,'0-exit',value='999999999999.000000000000000000000001')
    replace_record(path,'0-cost',value='0')
    with pytest.raises(ValueError,match='rounding_changes_label'): build(path)


def test_aggregate_age_matches_existing_fitter_contract(tmp_path):
    path=tmp_path/'f.db'; p=populate(path)
    # A source with fresh price/cost and another source with an older feature:
    # the parent fitter applies each source's limit to the conservative row age.
    with sqlite3.connect(path) as db:
        db.execute('DROP TRIGGER IF EXISTS feature_observations_update_blocked')
        db.execute("UPDATE feature_observations SET source_id='predictor' WHERE feature='signal'")
    with sqlite3.connect(path) as db:
        db.execute('CREATE TRIGGER feature_observations_update_blocked BEFORE UPDATE ON feature_observations BEGIN SELECT RAISE(ABORT,\'blocked\'); END')
    replace_record(path,'0-f',observed_at=(T-timedelta(seconds=20)).isoformat(),available_at=(T-timedelta(seconds=20)).isoformat())
    p['features'][0]['source_id']='predictor'
    gs=(replace(grants()[0],max_age_seconds=10),replace(grants()[0],source_id='predictor',max_age_seconds=60))
    with pytest.raises(ValueError,match='aggregate_feature_age'): build(path,p,gs)


def test_lineage_schema_bounds_are_checked(tmp_path):
    path=tmp_path/'f.db'; populate(path); package=inspect_package(build(path))
    changed=copy.deepcopy(package); changed['schema']='other'
    with pytest.raises(ValueError,match='package_schema'):
        fd.validate_training_package(rehash(changed),source_grants=grants(),now=T+timedelta(days=1))
    changed=copy.deepcopy(package); changed['observations']=[]
    with pytest.raises(ValueError,match='lineage_bound'):
        fd.validate_training_package(rehash(changed),source_grants=grants(),now=T+timedelta(days=1))
    changed=copy.deepcopy(package); changed['observations'][0]['extra']=1
    with pytest.raises(ValueError,match='lineage_schema'):
        fd.validate_training_package(rehash(changed),source_grants=grants(),now=T+timedelta(days=1))


def test_endpoint_time_rechecked_independently_of_selector(tmp_path,monkeypatch):
    path=tmp_path/'f.db'; populate(path)
    original=fd.ReadOnlyFeatureSource._point
    def wrong_endpoint(row):
        result=original(row)
        return replace(result,observed_at=T+timedelta(seconds=59)) if row['observation_id']=='0-exit' else result
    monkeypatch.setattr(fd.ReadOnlyFeatureSource,'_point',staticmethod(wrong_endpoint))
    with pytest.raises(ValueError,match='endpoint_time'): build(path)


def named_refusal(call, reason):
    try:
        call()
    except Exception as error:  # noqa: BLE001 - assert the precise refusal, not arbitrary failure
        result=(type(error).__name__,str(error))
    else:
        result=None
    assert result == ('ValueError','feature_dataset_'+reason)


@pytest.mark.parametrize('reason',[
    'input_bound','nonfinite_json','identity','numeric_text','numeric_bound','typed_grants',
    'duplicate_grant','source_not_permitted','grant_scope','rule_schema','rule_age','decision_schema',
    'feature_count','decision_bound'])
def test_rejection_contract_is_specific(reason):
    p=plan(); gs=grants(); call=None
    if reason=='input_bound': call=lambda:fd.decode(b'',10)
    if reason=='nonfinite_json': call=lambda:fd.decode(b'NaN',10)
    if reason=='identity': call=lambda:fd._id('spaces not allowed')
    if reason=='numeric_text': call=lambda:fd._number(2)
    if reason=='numeric_bound': call=lambda:fd._number('1e13')
    if reason=='typed_grants': gs=({'not':'a grant'},)
    if reason=='duplicate_grant': gs=gs+gs
    if reason=='source_not_permitted': gs=(replace(gs[0],rights_status=RightsStatus.UNVERIFIED),)
    if reason=='grant_scope': gs=gs+(replace(gs[0],source_id='extra'),)
    if reason=='rule_schema': p['features'][0]['unknown']='x'
    if reason=='rule_age': p['features'][0]['max_age_seconds']=61
    if reason=='decision_schema': p['decisions'][0]['unknown']='x'
    if reason=='feature_count': p['features']=[]
    if reason=='decision_bound': p['decisions']=[]
    if call is None: call=lambda:fd._plan(fd.canonical(p),gs,T+timedelta(days=1))
    named_refusal(call,reason)


def test_missing_endpoint_has_named_refusal(tmp_path):
    path=tmp_path/'f.db';populate(path)
    rewrite(path,"DELETE FROM feature_observations WHERE observation_id='0-exit'")
    named_refusal(lambda:build(path),'exact_endpoint_missing')


@pytest.mark.parametrize('reason',['lineage_bound','duplicate_lineage','lineage_replay'])
def test_package_has_specific_lineage_refusals(tmp_path,reason):
    path=tmp_path/'f.db';populate(path);p=inspect_package(build(path))
    if reason=='lineage_bound': p['observations']=[]
    if reason=='duplicate_lineage': p['observations'].append(copy.deepcopy(p['observations'][0]))
    if reason=='lineage_replay': p['data']['rows'][0]['gross_return']='1'
    named_refusal(lambda:fd.validate_training_package(rehash(p),source_grants=grants(),now=T+timedelta(days=1)),reason)


def test_cost_rejection_is_named(tmp_path):
    path=tmp_path/'f.db';populate(path);replace_record(path,'0-cost',value='-0.1')
    named_refusal(lambda:build(path),'price_cost_value')


def test_true_profit_after_cost_not_gross_is_preserved(tmp_path):
    path=tmp_path/'f.db';populate(path);replace_record(path,'0-cost',value='.03')
    row=inspect_package(build(path))['data']['rows'][0]
    assert D(row['gross_return']) > 0 and D(row['gross_return'])-D(row['cost_fraction']) < 0


@pytest.mark.parametrize('reason',[
    'plan_schema','cost_policy','future_cutoff','time_bound','feature_names',
    'price_cost_categories','price_cost_binding','role_collision','decision_order',
    'duplicate_or_overlap','outcome_not_due'])
def test_plan_rejection_contracts_are_specific(reason):
    p=plan(2)
    if reason=='plan_schema': p['partition']='holdout'
    if reason=='cost_policy': p['cost_policy_sha256']='invalid'
    if reason=='future_cutoff': p['cutoff']=(T+timedelta(days=2)).isoformat()
    if reason=='time_bound': p['horizon_seconds']=0
    if reason=='feature_names': p['features'].append(copy.deepcopy(p['features'][0]))
    if reason=='price_cost_categories': p['cost']['category']='MARKET'
    if reason=='price_cost_binding': p['cost']['schema_id']='wrong'
    if reason=='role_collision': p['cost']['feature']='price'
    if reason=='decision_order': p['decisions'].reverse()
    if reason=='duplicate_or_overlap': p['decisions'][1]['row_id']='r0'
    if reason=='outcome_not_due': p['cutoff']=(T+timedelta(seconds=59)).isoformat()
    named_refusal(lambda:fd._plan(fd.canonical(p),grants(),T+timedelta(days=1)),reason)


def test_endpoint_digest_is_checked_before_use(tmp_path):
    path=tmp_path/'f.db';populate(path)
    rewrite(path,"UPDATE feature_observations SET value='777' WHERE observation_id='0-exit'")
    try:
        build(path)
    except Exception as error:  # noqa: BLE001 - test requires the specific original integrity refusal
        actual=(type(error).__name__,str(error))
    else:
        actual=None
    assert actual==('ValueError','feature_observation_hash_mismatch')
