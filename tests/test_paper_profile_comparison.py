"""Synthetic recorded cohorts only: no host data, provider, fitting or execution."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant_ai.learning.feature_dataset import canonical
from quant_ai.learning.forecast_evaluation import _digest
from quant_ai.operations.paper_profile_comparison import compare_paper_profiles

START = datetime(2026, 1, 12, 4, tzinfo=timezone.utc)
NOW = START + timedelta(days=2)


def fixture():
    plan = {'schema': 'pramana.paper_profile_plan.v1', 'mode': 'PAPER', 'venue': 'NSE_CASH', 'event': 'positive_long_return_after_cost',
            'frozen_at': (START - timedelta(days=1)).isoformat(), 'start': START.isoformat(),
            'end': (START + timedelta(days=1)).isoformat(), 'catalog': ['A', 'B', 'C'],
            'fixed_symbols': ['A', 'B'], 'dynamic_limit': 2, 'horizon_seconds': 3600,
            **{k: 'a' * 64 for k in ('strategy_sha256', 'risk_policy_sha256', 'cost_policy_sha256', 'dynamic_rule_sha256')}}
    rows = []
    for symbol, gross, cost in [('A', '0.02', '0.03'), ('B', '0.1', '0.01'), ('C', '0.03', '0.01')]:
        profiles = {}
        for name, members, p in [('fixed', {'A', 'B'}, '0.8'), ('dynamic', {'A', 'C'}, '0.2')]:
            selected = symbol in members
            profiles[name] = {'status': 'HOLD' if selected else 'UNSELECTED',
                              'probability': p if selected else None,
                              'forecast_available_at': START.isoformat() if selected else None,
                              'reasons': ['strategy'] if selected else []}
        rows.append({'symbol': symbol, 'input_available_at': (START - timedelta(seconds=1)).isoformat(),
                     'proof_sha256': 'b' * 64, 'resolve_after': (START + timedelta(hours=1)).isoformat(),
                     'outcome': {'resolved_at': (START + timedelta(hours=2)).isoformat(),
                                 'gross_return': gross, 'cost_return': cost, 'evidence_sha256': 'c' * 64},
                     'profiles': profiles})
    cycles = [{'cycle_id': 'cycle-1', 'plan_sha256': _digest(canonical(plan)), 'decision_at': START.isoformat(),
               'selection_cutoff': (START - timedelta(days=1)).isoformat(),
               'selection_available_at': (START - timedelta(minutes=1)).isoformat(),
               'selection_sha256': 'd' * 64, 'dynamic_selected': ['A', 'C'], 'opportunities': rows}]
    return plan, cycles


def compare(plan=None, cycles=None, **changes):
    if plan is None:
        plan, cycles = fixture()
    return compare_paper_profiles(plan, cycles, now=changes.get('now', NOW))


def test_reproducible_cost_aware_cohorts_and_common_forecasts_with_no_io_or_fitting(monkeypatch):
    import sqlite3

    import quant_ai.learning.forecast_evaluation as scoring
    from quant_ai.learning.outcomes import ForecastOutcomeJournal
    monkeypatch.setattr(sqlite3, 'connect', lambda *a, **k: pytest.fail('database forbidden'))
    monkeypatch.setattr(scoring, 'fit_candidate', lambda *a, **k: pytest.fail('fit forbidden'))
    monkeypatch.setattr(ForecastOutcomeJournal, '__init__', lambda *a, **k: pytest.fail('journal constructor forbidden'))
    plan, cycles = fixture()
    original = deepcopy((plan, cycles))
    report = compare(plan, cycles)
    assert (plan, cycles) == original
    assert report == compare(plan, cycles)
    assert report['catalog_observations'] == 3
    assert report['cohorts']['fixed']['selected_observations'] == 2
    assert report['cohorts']['dynamic']['selected_observations'] == 2
    assert report['cohorts']['fixed']['mean_after_cost_opportunity_return'] == '0.04'
    assert report['cohorts']['dynamic']['mean_after_cost_opportunity_return'] == '0.005'
    assert report['matched_forecasts']['pairs'] == 1  # A only, label is negative AFTER costs.
    assert Decimal(report['matched_forecasts']['fixed']['brier_score']) == Decimal('0.64')
    assert Decimal(report['matched_forecasts']['dynamic']['brier_score']) == Decimal('0.04')
    assert report['cohorts']['fixed']['constant_half_baseline']['brier_score'] == '0.25'
    assert report['cohorts']['fixed']['reasons'] == {'strategy': 2}
    assert report['selection_counts'][0]['selection_age_seconds'] == '60.0'
    for field in ('activation_authorized', 'trading_authorized', 'model_updated', 'portfolio_returns_evaluated',
                  'source_authenticity_verified', 'prospective_registration_verified', 'matched_sample_at_least_30'):
        assert report[field] is False


def test_pending_missing_forecasts_and_empty_dynamic_selection_stay_in_denominators():
    plan, cycles = fixture()
    rows = cycles[0]['opportunities']
    rows[0]['outcome'] = None
    rows[1]['profiles']['fixed'].update(probability=None, forecast_available_at=None)
    report = compare(plan, cycles)
    fixed = report['cohorts']['fixed']
    assert fixed['selected_observations'] == 2 and fixed['unresolved_opportunities'] == 1
    assert fixed['resolved_without_forecast'] == 1 and fixed['forecast_metrics'] is None
    assert report['matched_forecasts'] is None
    cycles[0]['dynamic_selected'] = []
    for row in rows:
        row['profiles']['dynamic'] = {'status': 'UNSELECTED', 'probability': None, 'forecast_available_at': None, 'reasons': []}
    assert compare(plan, cycles)['cohorts']['dynamic']['mean_after_cost_opportunity_return'] is None


@pytest.mark.parametrize('field,value', [('mode', 'LIVE'), ('venue', 'CRYPTO'), ('frozen_at', START.isoformat()),
    ('dynamic_limit', True), ('dynamic_limit', 16), ('horizon_seconds', True),
    ('fixed_symbols', ['UNKNOWN']), ('catalog', ['A', 'A']), ('cost_policy_sha256', 'bad')])
def test_invalid_frozen_profiles_refused(field, value):
    plan, cycles = fixture()
    plan[field] = value
    with pytest.raises(ValueError):
        compare(plan, cycles)


@pytest.mark.parametrize('change', ['future_selection', 'current_session_cutoff', 'unknown_selection',
    'over_limit', 'missing_subject', 'duplicate_subject', 'future_input', 'future_forecast', 'early_outcome',
    'future_outcome', 'bad_cost', 'partial_outcome', 'malformed_probability', 'unselected_probability',
    'unknown_input_proposal', 'missing_refusal_reason', 'wrong_membership', 'changed_horizon', 'duplicate_cycle'])
def test_invalid_or_leaking_observations_refused(change):
    plan, cycles = fixture()
    cycle, row = cycles[0], cycles[0]['opportunities'][0]
    fixed = row['profiles']['fixed']
    if change == 'future_selection': cycle['selection_available_at'] = (START + timedelta(seconds=1)).isoformat()
    if change == 'current_session_cutoff': cycle['selection_cutoff'] = START.isoformat()
    if change == 'unknown_selection': cycle['dynamic_selected'] = ['UNKNOWN']
    if change == 'over_limit': cycle['dynamic_selected'] = ['A', 'B', 'C']
    if change == 'missing_subject': cycle['opportunities'].pop()
    if change == 'duplicate_subject': cycle['opportunities'][1] = deepcopy(row)
    if change == 'future_input': row['input_available_at'] = (START + timedelta(seconds=1)).isoformat()
    if change == 'future_forecast': fixed['forecast_available_at'] = (START + timedelta(seconds=1)).isoformat()
    if change == 'early_outcome': row['outcome']['resolved_at'] = START.isoformat()
    if change == 'future_outcome': row['outcome']['resolved_at'] = (NOW + timedelta(seconds=1)).isoformat()
    if change == 'bad_cost': row['outcome']['cost_return'] = '-0.01'
    if change == 'partial_outcome': del row['outcome']['cost_return']
    if change == 'malformed_probability': fixed['probability'] = True
    if change == 'unselected_probability': cycle['opportunities'][2]['profiles']['fixed']['probability'] = '0.5'
    if change == 'unknown_input_proposal':
        row['input_available_at'] = None
        fixed.update(status='PROPOSAL', probability=None, forecast_available_at=None, reasons=[])
        row['profiles']['dynamic'].update(probability=None, forecast_available_at=None)
    if change == 'missing_refusal_reason': fixed['reasons'] = []
    if change == 'wrong_membership': fixed['status'] = 'UNSELECTED'
    if change == 'changed_horizon': row['resolve_after'] = (START + timedelta(hours=2)).isoformat()
    if change == 'duplicate_cycle': cycles.append(deepcopy(cycle))
    with pytest.raises(ValueError):
        compare(plan, cycles)


def test_half_open_window_and_partial_window_are_explicit():
    plan, cycles = fixture()
    report = compare(plan, cycles, now=START + timedelta(hours=3))
    assert report['window_complete'] is False
    plan['end'] = START.isoformat()
    with pytest.raises(ValueError):
        compare(plan, cycles)


def test_distinct_cycle_cannot_double_count_same_decision_boundary():
    plan, cycles = fixture()
    extra = deepcopy(cycles[0])
    extra['cycle_id'] = 'cycle-2'
    cycles.append(extra)
    with pytest.raises(ValueError, match='cycle_order'):
        compare(plan, cycles)


def model_pair():
    item = {'schema': 'pramana.model_comparison.v1', 'pair_id': 'pair-1', 'observed_at': START.isoformat(),
            'prompt_sha256': 'a' * 64, 'execution_authority': 'primary_only',
            'confidence_is_calibrated_probability': False, 'outcome_status': 'not_evaluated'}
    for role, provider, stance in [('primary', 'anthropic', 'NEUTRAL'), ('challenger', 'openai', 'BUY')]:
        item[role] = {'provider': provider, 'status': 'completed', 'prompt_sha256': 'a' * 64,
                      'started_at': START.isoformat(), 'completed_at': (START + timedelta(seconds=1)).isoformat(),
                      'duration_ms': 1000, 'usage': {'input_tokens': 10, 'output_tokens': 20},
                      'consensus': {'stance': stance, 'confidence': 0.9}}
    return item


def summarize(pairs):
    from quant_ai.operations.paper_profile_comparison import summarize_recorded_model_pairs
    return summarize_recorded_model_pairs(pairs, start=START, end=START + timedelta(days=1), now=NOW)


def test_recorded_model_comparison_is_descriptive_only_and_does_not_echo_payloads():
    pair = model_pair()
    original = deepcopy(pair)
    pair['primary']['consensus']['private_fixture'] = 'never report this'
    report = summarize([pair])
    assert report['completed_same_evidence_pairs'] == report['stance_disagreements'] == 1
    assert report['roles']['primary']['input_tokens'] == 10
    assert report['roles']['challenger']['mean_duration_ms'] == '1000'
    assert not report['forecast_skill_evaluated'] and not report['provider_calls_made']
    assert report['execution_authority'] == 'primary_only' and not report['winner_selected']
    assert 'never report this' not in str(report)
    del pair['primary']['consensus']['private_fixture']
    assert pair == original


@pytest.mark.parametrize('change,reason', [('missing', 'incomplete_pair'), ('budget', 'incomplete_pair'),
    ('hash', 'evidence_mismatch_or_unknown'), ('time', 'completion_time_or_stance_unknown'),
    ('stance', 'completion_time_or_stance_unknown')])
def test_unusable_model_pairs_stay_counted(change, reason):
    pair = model_pair()
    if change == 'missing': del pair['challenger']
    if change == 'budget': pair['challenger']['status'] = 'budget_exhausted'
    if change == 'hash': pair['challenger']['prompt_sha256'] = 'b' * 64
    if change == 'time': pair['challenger']['completed_at'] = (NOW + timedelta(seconds=1)).isoformat()
    if change == 'stance': del pair['challenger']['consensus']['stance']
    report = summarize([pair])
    assert report['recorded_pairs'] == 1 and report['completed_same_evidence_pairs'] == 0
    assert report['unusable_pair_reasons'] == {reason: 1}


def test_partial_usage_and_invalid_latency_are_unknown_not_zero_samples():
    pair = model_pair()
    pair['primary']['usage']['output_tokens'] = None
    pair['primary']['duration_ms'] = True
    result = summarize([pair])
    assert result['completed_same_evidence_pairs'] == 1
    assert result['roles']['primary']['complete_usage_samples'] == 0
    assert result['roles']['primary']['latency_samples'] == 0
    assert result['roles']['primary']['mean_duration_ms'] is None


def test_duplicate_model_pairs_and_changed_execution_authority_refuse():
    pair = model_pair()
    with pytest.raises(ValueError):
        summarize([pair, deepcopy(pair)])
    pair['execution_authority'] = 'challenger'
    with pytest.raises(ValueError):
        summarize([pair])


def test_changed_profile_requires_its_own_recorded_plan_binding():
    plan, cycles = fixture()
    plan['risk_policy_sha256'] = 'f' * 64
    with pytest.raises(ValueError, match='plan_binding'):
        compare(plan, cycles)


def test_malformed_model_status_and_stance_remain_unknown():
    pair = model_pair()
    pair['challenger']['status'] = []
    assert summarize([pair])['unusable_pair_reasons'] == {'incomplete_pair': 1}
    pair = model_pair()
    pair['challenger']['consensus']['stance'] = []
    assert summarize([pair])['unusable_pair_reasons'] == {'completion_time_or_stance_unknown': 1}


def test_valid_empty_proposal_reasons_do_not_become_unknown():
    plan, cycles = fixture()
    cycles[0]['opportunities'][0]['profiles']['fixed'].update(status='PROPOSAL', reasons=[])
    result = compare(plan, cycles)
    assert result['cohorts']['fixed']['statuses'] == {'HOLD': 1, 'PROPOSAL': 1}
    assert result['cohorts']['fixed']['reasons'] == {'strategy': 1}


def test_end_boundary_is_excluded_even_with_otherwise_valid_plan():
    plan, cycles = fixture()
    cycles[0]['decision_at'] = plan['end']
    with pytest.raises(ValueError, match='selection_time'):
        compare(plan, cycles)


def test_summary_consumes_actual_mocked_challenger_provenance_without_new_calls():
    import asyncio

    from test_astra_challenger import client, payload, sdk

    from quant_ai.llm.anthropic_client import AnthropicSwarmClient
    from quant_ai.llm.challenger import ChallengerConsensusClient
    from quant_ai.operations.paper_profile_comparison import summarize_recorded_model_pairs

    began = datetime.now(timezone.utc)
    primary = AnthropicSwarmClient(client=sdk(payload(stance='NEUTRAL')))
    challenger, requests = client()
    wrapper = ChallengerConsensusClient(primary, challenger)
    result = asyncio.run(wrapper.generate_trading_consensus('synthetic same evidence'))
    pair = result.provenance['model_comparison']
    now = datetime.now(timezone.utc)
    report = summarize_recorded_model_pairs([pair], start=began, end=now + timedelta(seconds=1), now=now)
    assert report['completed_same_evidence_pairs'] == 1
    assert report['execution_authority'] == 'primary_only'
    assert len(requests) == 1 and primary._client.messages.create.await_count == 1
    assert result['stance'] == 'NEUTRAL'
