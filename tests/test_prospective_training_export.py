"""Synthetic contracts only: no genuine dataset, broker, provider or model approval."""
import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from test_offline_logistic_fitting import NOW, dataset, fit

from quant_ai.learning import fitting, shadow
from quant_ai.learning.contracts import AccessPlane, KnowledgeCategory, RightsStatus, SourceGrant
from quant_ai.learning.prospective import validate_plan


def v2_data():
    data=dataset()
    data.update(schema=fitting.SCHEMA_V2,source_ids=['zerodha'],feature_names=['market_spread_bps'],
                maximum_feature_age_seconds=60,endpoint_policy_id=shadow.ENDPOINT_POLICY,
                maximum_endpoint_age_seconds=60,cost_return='0.01',cost_policy_id='fixed_shadow_round_trip.v1')
    data['cost_policy_sha256']=shadow._hash({'id':data['cost_policy_id'],'cost_return':data['cost_return']})
    for i,row in enumerate(data['rows']):
        due=shadow._instant(row['resolve_after'])
        row.update(source_ids=['zerodha'],values={'market_spread_bps':'2' if i%2 else '0'},
                   reference_price='100',endpoint_price='104' if i%2 else '100.4',
                   endpoint_observed_at=(due-timedelta(seconds=1)).isoformat(),
                   endpoint_available_at=row['outcome_available_at'])
    return data


def source_grant():
    return SourceGrant('zerodha','SYNTHETIC-only-not-provider-attestation',
        frozenset({KnowledgeCategory.MARKET}),frozenset({AccessPlane.TRAINING}),
        True,RightsStatus.INTERNAL,60)


def fit_v2(data=None,grants=None):
    return fitting.fit_candidate(json.dumps(data or v2_data()).encode(),
        source_grants=grants or (source_grant(),),run_id='synthetic-v2-run',
        candidate_id='synthetic-v2-candidate',clock=lambda:NOW)


def test_real_fitter_exports_compatible_frozen_v2_without_relabeling_v1():
    result=fit_v2()
    model=result.bundle.validate()
    assert model['schema']==shadow.MODEL_SCHEMA_V2
    assert model['endpoint_policy_id']==shadow.ENDPOINT_POLICY
    assert result.diagnostics['positive_after_cost_rows']==30
    assert result.bundle.dataset.label_schema_sha256==shadow.label_schema_digest(model)
    raw=fitting.export_prospective_plan(result,cost_return='0.01',clock=lambda:NOW)
    plan,bundle=validate_plan(raw,shadow._sha(raw))
    assert bundle.payload()==result.bundle.payload()
    assert plan['frozen_at']==NOW.isoformat() and plan['cost_return']=='0.01'
    assert result.diagnostics['trading_authorized'] is False
    assert result.diagnostics['source_authenticity_verified'] is False
    original=fit()
    before=original.bundle.payload()
    with pytest.raises(ValueError,match='v2_candidate_required'):
        fitting.export_prospective_plan(original,cost_return='0.01',clock=lambda:NOW)
    assert original.bundle.payload()==before


@pytest.mark.parametrize('change,reason',[
    ({'endpoint_price':'105'},'endpoint_return_binding'),
    ({'gross_return':'0.5'},'endpoint_return_binding'),
    ({'reference_price':'0'},'endpoint_price_cost'),
    ({'cost_fraction':'0.005'},'endpoint_price_cost'),
    ({'values':{'market_spread_bps':'-1'}},'quote_geometry'),
])
def test_price_witnesses_cost_and_quote_values_bind_the_training_target(change,reason):
    data=v2_data();data['rows'][0].update(change)
    with pytest.raises(ValueError,match=reason):
        fit_v2(data)


@pytest.mark.parametrize('case',['at_decision','after_horizon','stale','received_before_observation','received_after_outcome','missing'])
def test_invalid_endpoint_witnesses_refuse_before_fitting(case):
    data=v2_data();row=data['rows'][0];due=shadow._instant(row['resolve_after'])
    if case=='at_decision':row['endpoint_observed_at']=row['decision_at']
    elif case=='after_horizon':row['endpoint_observed_at']=(due+timedelta(seconds=1)).isoformat()
    elif case=='stale':row['endpoint_observed_at']=(due-timedelta(seconds=61)).isoformat()
    elif case=='received_before_observation':row['endpoint_available_at']=(due-timedelta(seconds=2)).isoformat()
    elif case=='received_after_outcome':row['endpoint_available_at']=(due+timedelta(seconds=3)).isoformat()
    else:del row['endpoint_price']
    with pytest.raises(ValueError,match='endpoint_witness_time|row_schema'):
        fit_v2(data)


@pytest.mark.parametrize('change,reason',[
    ({'cost_return':'0.02'},'frozen_cost_policy'),
    ({'maximum_endpoint_age_seconds':True},'endpoint_policy'),
    ({'maximum_feature_age_seconds':61},'prospective_quote_age'),
    ({'endpoint_policy_id':'arbitrary'},'endpoint_policy'),
    ({'feature_names':['technical_signal']},'prospective_quote_scope'),
    ({'feature_names':None},'feature_schema'),
])
def test_v2_scope_is_explicit_and_never_inferred_from_a_v1_label(change,reason):
    data=v2_data();data.update(change)
    with pytest.raises(ValueError,match=reason):fit_v2(data)


def test_quote_price_feature_and_declared_grant_match_serving_scope():
    data=v2_data();data['feature_names']=['market_last_price']
    for row in data['rows']:row['values']={'market_last_price':'101'}
    with pytest.raises(ValueError,match='quote_geometry'):fit_v2(data)
    grant=replace(source_grant(),categories=frozenset({KnowledgeCategory.FUNDAMENTAL}))
    with pytest.raises(ValueError,match='prospective_quote_scope'):fit_v2(grants=(grant,))


def test_plan_export_refuses_changed_cost_or_backdated_freeze():
    result=fit_v2()
    with pytest.raises(ValueError,match='frozen_cost_policy'):
        fitting.export_prospective_plan(result,cost_return='0.010',clock=lambda:NOW)
    with pytest.raises(ValueError,match='plan_precedes_training'):
        fitting.export_prospective_plan(result,cost_return='0.01',clock=lambda:NOW-timedelta(seconds=1))


def test_cli_exports_one_private_plan_and_refuses_overwriting(tmp_path):
    root=Path(__file__).resolve().parents[1]
    data=tmp_path/'synthetic-data.json';data.write_text(json.dumps(v2_data()));data.chmod(0o600)
    grants=tmp_path/'synthetic-grants.json'
    grants.write_text(json.dumps([{'source_id':'zerodha','provider':'synthetic-only',
        'categories':['MARKET'],'planes':['TRAINING'],'point_in_time':True,
        'rights_status':'INTERNAL','max_age_seconds':60}]))
    grants.chmod(0o600)
    output=tmp_path/'synthetic-plan.json'
    command=[sys.executable,'-B',str(root/'scripts/fit_shadow_candidate.py'),'--input',str(data),
        '--grants',str(grants),'--output',str(output),'--run-id','synthetic-run',
        '--candidate-id','synthetic-candidate','--prospective-plan']
    env={'PATH':os.environ.get('PATH',''),'TRADING_LIVE_MONEY_ACTIVE':'false',
         'PYTHONDONTWRITEBYTECODE':'1'}
    first=subprocess.run(command,capture_output=True,text=True,check=False,env=env,timeout=30)
    assert first.returncode==0,first.stderr
    raw=output.read_bytes();summary=json.loads(first.stdout)
    validate_plan(raw,summary['prospective_plan_sha256'])
    assert output.stat().st_mode & 0o077==0
    assert summary['trading_authorized'] is False and summary['source_authenticity_verified'] is False
    assert subprocess.run(command,capture_output=True,text=True,check=False,env=env,timeout=30).returncode==2
    assert output.read_bytes()==raw


@pytest.mark.parametrize('case', [
    'dataset_schema', 'dataset_schema:2', 'endpoint_policy', 'prospective_quote_scope',
    'frozen_cost_policy', 'frozen_cost_policy:2', 'prospective_quote_scope:2',
    'prospective_quote_scope:3', 'prospective_quote_age', 'endpoint_witness_time',
    'endpoint_price_cost', 'quote_geometry', 'rounding_changes_label',
    'endpoint_return_binding', 'v2_candidate_required',
])
def test_v2_guard_has_an_explicit_assertion_boundary(case):
    data=v2_data(); grants=(source_grant(),)
    if case=='dataset_schema': data=[]
    elif case=='dataset_schema:2': data['schema']='unknown'
    elif case=='endpoint_policy': data['maximum_endpoint_age_seconds']=True
    elif case=='prospective_quote_scope':
        data['source_ids']=['other']
        for row in data['rows']: row['source_ids']=['other']
        grants=(replace(source_grant(),source_id='other'),)
    elif case=='frozen_cost_policy': data['cost_return']=0.01
    elif case=='frozen_cost_policy:2': data['cost_policy_sha256']='0'*64
    elif case=='prospective_quote_scope:2':
        grants=(replace(source_grant(),categories=frozenset({KnowledgeCategory.FUNDAMENTAL})),)
    elif case=='prospective_quote_scope:3':
        data['feature_names']=['signal']
        for row in data['rows']: row['values']={'signal':'0'}
    elif case=='prospective_quote_age': data['maximum_feature_age_seconds']=61
    elif case=='endpoint_witness_time': data['rows'][0]['endpoint_observed_at']=data['rows'][0]['decision_at']
    elif case=='endpoint_price_cost': data['rows'][0]['cost_fraction']='0.005'
    elif case=='quote_geometry': data['rows'][0]['values']['market_spread_bps']='-1'
    elif case=='rounding_changes_label':
        data['rows'][0].update(reference_price='3',endpoint_price='3.030000000000000000000001',gross_return='0.01')
    elif case=='endpoint_return_binding': data['rows'][0]['endpoint_price']='105'
    error=None
    try:
        if case=='v2_candidate_required':
            fitting.export_prospective_plan(fit(),cost_return='0.01',clock=lambda:NOW)
        else:
            fitting.fit_candidate(json.dumps(data).encode(),source_grants=grants,
                run_id='guard-run',candidate_id='guard-candidate',clock=lambda:NOW)
    except Exception as exc:  # noqa: BLE001 - mutations must fail by assertion, including unexpected errors
        error=(type(exc),str(exc))
    assert error==(ValueError,'logistic_fit_'+case.split(':')[0])
