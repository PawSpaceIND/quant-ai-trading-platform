"""Explicit guard inventory; paired control/assertion failures on disposable copies."""
from __future__ import annotations

import ast
import hashlib
import json
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

import pytest
from test_kite_history_mutations import (
    test_kite_history_guard_requires_passing_control_and_failing_mutant as run_guard,
)

ROOT = Path(__file__).resolve().parents[1]
MODULE = "src/quant_ai/learning/fitting.py"
TEST = "tests/test_offline_logistic_fitting.py::"
TARGETS = {
 "numeric_precision_bound": "test_numeric_precision_has_a_direct_boundary",
 "sources_required": "test_source_list_has_direct_boundaries",
 "duplicate_source": "test_source_list_has_direct_boundaries",
 "regularization_bound": "test_optimizer_configuration_is_bounded",
 "tolerance_bound": "test_optimizer_configuration_is_bounded",
 "iteration_bound": "test_optimizer_configuration_is_bounded",
 "input_size": "test_input_size_has_a_direct_boundary",
 "dataset_schema": "test_rejects_bad_training_evidence_before_fit[schema]",
 "training_partition_required": "test_rejects_bad_training_evidence_before_fit[partition]",
 "cost_digest": "test_rejects_bad_training_evidence_before_fit[cost_digest]",
 "future_dataset": "test_rejects_bad_training_evidence_before_fit[future_cutoff]",
 "typed_source_grants_required": "test_source_permissions_are_required_declarations",
 "duplicate_grant": "test_source_permissions_are_required_declarations",
 "source_grant_scope": "test_source_permissions_are_required_declarations",
 "training_source_not_permitted": "test_source_permissions_are_required_declarations",
 "feature_schema": "test_rejects_bad_training_evidence_before_fit[empty_features]",
 "feature_schema:2": "test_rejects_bad_training_evidence_before_fit[illegal_name]",
 "time_budget": "test_rejects_bad_training_evidence_before_fit[horizon]",
 "row_count": "test_rejects_bad_training_evidence_before_fit[small]",
 "work_budget": "test_work_budget_is_checked_before_rows_are_fit",
 "row_schema": "test_rejects_bad_training_evidence_before_fit[row_schema]",
 "future_or_stale_feature": "test_rejects_bad_training_evidence_before_fit[future_feature]",
 "outcome_not_known_at_cutoff": "test_rejects_bad_training_evidence_before_fit[unknown_label]",
 "nonchronological_rows": "test_rejects_bad_training_evidence_before_fit[order]",
 "duplicate_row": "test_rejects_bad_training_evidence_before_fit[duplicate]",
 "overlapping_outcomes": "test_rejects_bad_training_evidence_before_fit[overlap]",
 "unknown_row_source": "test_rejects_bad_training_evidence_before_fit[unknown_source]",
 "source_age_limit": "test_source_permissions_are_required_declarations",
 "row_feature_schema": "test_rejects_bad_training_evidence_before_fit[row_features]",
 "return_or_cost_bound": "test_rejects_bad_training_evidence_before_fit[negative_cost]",
 "unused_declared_source": "test_rejects_bad_training_evidence_before_fit[unused_source]",
 "class_support": "test_rejects_bad_training_evidence_before_fit[single_class]",
 "logit_bound": "test_numeric_logit_bound_precedes_optimization_arithmetic",
 "invalid_optimization": "test_worsening_objective_is_not_published",
 "typed_config_required": "test_config_requires_its_typed_contract",
 "completion_clock_reversed": "test_completion_clock_cannot_move_back",
 "implementation_changed": "test_changed_implementation_is_not_certified",
}


def cases():
    source=(ROOT/MODULE).read_text()
    calls=sorted([n for n in ast.walk(ast.parse(source)) if isinstance(n,ast.Call)
        and isinstance(n.func,ast.Name) and n.func.id=="_require"], key=lambda n:n.lineno)
    seen=Counter(); result=[]
    for call in calls:
        reason=call.args[1].value; seen[reason]+=1
        key=reason if seen[reason]==1 else f"{reason}:{seen[reason]}"
        assert key in TARGETS, "A new guard requires an explicit regression target"
        result.append({"id":key,"path":MODULE,"old":ast.get_source_segment(source,call),
                       "new":"None","test":TEST+TARGETS[key]})
    assert {c["id"] for c in result}==set(TARGETS)
    result.append({"id":"convergence_refusal","path":MODULE,
        "old":'raise ValueError("logistic_fit_not_converged")',
        "new":'return tuple(weights), intercept, {"converged": True}',
        "test":TEST+"test_nonconverged_candidate_is_refused"})
    script="scripts/fit_shadow_candidate.py"; cli=(ROOT/script).read_text()
    boundaries=[
        ("grant_list",'type(rows) is not list or not 1 <= len(rows) <= 64',"False","test_cli_grant_container_is_explicit"),
        ("grant_object",'type(row) is not dict',"False","test_cli_grant_container_is_explicit"),
        ("input_privacy",'not stat.S_ISREG(before.st_mode) or before.st_nlink != 1\n                or before.st_uid != os.geteuid() or stat.S_IMODE(before.st_mode) & 0o077\n                or before.st_size > maximum',"False","test_private_input_checks"),
        ("input_stability",'not raw or len(raw) > maximum or _identity(before) != _identity(os.fstat(handle.fileno()))\n                or _identity(before) != _identity(path.lstat())',"False","test_input_stat_change_is_not_adopted"),
        ("output_directory",'not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()\n            or stat.S_IMODE(info.st_mode) & 0o077',"False","test_output_parent_must_already_be_private"),
        ("output_preflight",'path.exists() or path.is_symlink()',"False","test_existing_output_refuses_before_creating_staging_file"),
        ("write_count",'handle.write(raw) != len(raw)','handle.write(raw) < 0',"test_failed_output_write_is_not_published[write_count]"),
        ("readback",'handle.read() != raw','handle.read() is None',"test_failed_output_write_is_not_published[readback]"),
        ("paper_only",'os.environ.get("TRADING_LIVE_MONEY_ACTIVE", "false") != "false"',"False","test_cli_refuses_live_money_mode_before_writing"),
        ("before_training_output",'args.output.exists() or args.output.is_symlink()',"False","test_cli_existing_output_refuses_before_trainer"),
        ("no_overwrite_publication",'os.link(temporary, path, follow_symlinks=False)','os.replace(temporary, path)',"test_concurrent_output_creator_is_preserved"),
    ]
    for key,old,new,target in boundaries:
        assert cli.count(old)==1,key
        result.append({"id":key,"path":script,"old":old,"new":new,"test":TEST+target})
    return result


CASES=cases()


@pytest.mark.parametrize("case",CASES,ids=[c["id"] for c in CASES])
def test_offline_fit_guard_requires_passing_control_and_assertion_failure(tmp_path,case):
    protected=(ROOT/MODULE,ROOT/"scripts/fit_shadow_candidate.py",ROOT/"tests/test_offline_logistic_fitting.py")
    before={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
    run_guard(tmp_path,case)
    failures=[(row.get("name"),failure.get("message", ""))
              for row in ET.parse(tmp_path/"mutant.xml").iter("testcase")
              for failure in row.findall("failure")]
    assert failures and all(name and message.startswith(("assert ","AssertionError:","Failed:"))
                            for name,message in failures), failures
    assert before=={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
    (tmp_path/"assertion-failures.json").write_text(json.dumps(failures))
