"""Reuse the existing copied-source harness; each new refusal has an assertion witness."""
from __future__ import annotations

import ast
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from test_kite_history_mutations import (
    test_kite_history_guard_requires_passing_control_and_failing_mutant as run_guard,
)

ROOT=Path(__file__).resolve().parents[1]
MODULE='src/quant_ai/learning/feature_dataset.py'
TEST='tests/test_feature_training_dataset.py::'
INTEGRATION='tests/test_feature_dataset_fit_integration.py::'
TARGETS={
    'input_bound': 'test_rejection_contract_is_specific[input_bound]',
    'aware_time': 'test_naive_clock_is_refused_before_any_source_work',
    'identity': 'test_rejection_contract_is_specific[identity]',
    'numeric_text': 'test_rejection_contract_is_specific[numeric_text]',
    'numeric_bound': 'test_rejection_contract_is_specific[numeric_bound]',
    'private_source': 'test_source_is_private_owned_unaliased_regular_file',
    'plan_schema': 'test_plan_rejection_contracts_are_specific[plan_schema]',
    'cost_policy': 'test_plan_rejection_contracts_are_specific[cost_policy]',
    'future_cutoff': 'test_plan_rejection_contracts_are_specific[future_cutoff]',
    'feature_count': 'test_rejection_contract_is_specific[feature_count]',
    'typed_grants': 'test_rejection_contract_is_specific[typed_grants]',
    'duplicate_grant': 'test_rejection_contract_is_specific[duplicate_grant]',
    'grant_scope': 'test_rejection_contract_is_specific[grant_scope]',
    'feature_names': 'test_plan_rejection_contracts_are_specific[feature_names]',
    'price_cost_categories': 'test_plan_rejection_contracts_are_specific[price_cost_categories]',
    'price_cost_binding': 'test_plan_rejection_contracts_are_specific[price_cost_binding]',
    'role_collision': 'test_plan_rejection_contracts_are_specific[role_collision]',
    'decision_bound': 'test_rejection_contract_is_specific[decision_bound]',
    'point_binding': 'test_point_schema_and_age_checked_after_selection',
    'observation_reuse': 'test_point_schema_and_age_checked_after_selection',
    'source_row_bound': 'test_source_count_limit_is_enforced',
    'data_size': 'test_package_and_dataset_size_limits',
    'package_size': 'test_package_and_dataset_size_limits',
    'package_schema': 'test_lineage_schema_bounds_are_checked',
    'package_digest': 'test_package_hash_is_required',
    'future_package': 'test_rehashed_package_must_replay_its_actual_selected_records[future_package]',
    'lineage_bound': 'test_package_has_specific_lineage_refusals[lineage_bound]',
    'lineage_replay': 'test_package_has_specific_lineage_refusals[lineage_replay]',
    'fit_grants': INTEGRATION+'test_fit_grants_have_named_refusal',
    'duplicate_json_key': 'test_duplicate_json_keys_and_extra_fields_are_refused',
    'source_replaced': 'test_replaced_source_path_refuses_even_if_connection_is_still_valid',
    'time_bound': 'test_plan_rejection_contracts_are_specific[time_bound]',
    'rule_schema': 'test_rejection_contract_is_specific[rule_schema]',
    'rule_age': 'test_rejection_contract_is_specific[rule_age]',
    'source_not_permitted': 'test_rejection_contract_is_specific[source_not_permitted]',
    'decision_schema': 'test_rejection_contract_is_specific[decision_schema]',
    'decision_order': 'test_plan_rejection_contracts_are_specific[decision_order]',
    'duplicate_or_overlap': 'test_plan_rejection_contracts_are_specific[duplicate_or_overlap]',
    'outcome_not_due': 'test_plan_rejection_contracts_are_specific[outcome_not_due]',
    'point_age': 'test_point_schema_and_age_checked_after_selection',
    'exact_endpoint_missing': 'test_missing_endpoint_has_named_refusal',
    'endpoint_time': 'test_endpoint_time_rechecked_independently_of_selector',
    'price_cost_value': 'test_cost_rejection_is_named',
    'aggregate_feature_age': 'test_aggregate_age_matches_existing_fitter_contract',
    'rounding_changes_label': 'test_rounding_cannot_flip_the_training_event',
    'return_bound': 'test_prices_and_costs_must_be_usable',
    'lineage_schema': 'test_lineage_schema_bounds_are_checked',
    'duplicate_lineage': 'test_package_has_specific_lineage_refusals[duplicate_lineage]',
    'nonfinite_json': 'test_rejection_contract_is_specific[nonfinite_json]',
}


def cases():
    source=(ROOT/MODULE).read_text()
    calls=[n for n in ast.walk(ast.parse(source)) if isinstance(n,ast.Call)
           and isinstance(n.func,ast.Name) and n.func.id=='_check']
    assert {n.args[1].value for n in calls} == set(TARGETS), 'Every new guard requires a mapped test'
    result=[]
    for call in calls:
        reason=call.args[1].value; target=TARGETS[reason]
        result.append(dict(id=reason,path=MODULE,old=ast.get_source_segment(source,call),new='None',
                           test=target if target.startswith('tests/') else TEST+target))
    result.extend([
        dict(id='single_snapshot',path=MODULE,old='store.db.execute("BEGIN")',new='None',
             test=TEST+'test_single_read_snapshot_ignores_concurrent_later_revision'),
        dict(id='endpoint_digest',path=MODULE,old='store._verify_row(row)',new='None',
             test=TEST+'test_endpoint_digest_is_checked_before_use'),
    ])
    script='scripts/build_feature_training_data.py'
    for reason,old,target in (
        ('paper_only','os.environ.get("TRADING_LIVE_MONEY_ACTIVE", "false") != "false"',
         'test_command_refuses_live_money_mode'),
        ('output_preflight','args.output.exists() or args.output.is_symlink()',
         'test_existing_output_refused_before_any_input_read'),
    ):
        result.append(dict(id=reason,path=script,old=old,new='False',test=INTEGRATION+target))
    return result


CASES=cases()


@pytest.mark.parametrize('case',CASES,ids=[c['id'] for c in CASES])
def test_feature_dataset_guard_requires_control_and_assertion_failure(tmp_path,case):
    run_guard(tmp_path,case)
    failures=[(row.get('name'),f.get('message','')) for row in ET.parse(tmp_path/'mutant.xml').iter('testcase')
              for f in row.findall('failure')]
    assert failures and all(name and message.startswith(('assert ','AssertionError:','Failed:'))
                            for name,message in failures), failures
