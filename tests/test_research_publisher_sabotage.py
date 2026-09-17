"""Deliberately broken isolated copies must fail the named real regression tests."""
from __future__ import annotations

import ast
import hashlib
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE = Path("src/quant_ai/backtesting/research_publisher.py")
CORE = "tests/test_research_publisher.py::"
GUARDS = "tests/test_research_publisher_guards.py::"
CASES = [
    ("full_sample", CORE + "test_full_sample_cannot_be_relabelled_holdout"),
    ("trial_count_reset", CORE + "test_actual_publisher_counts_all_trials_and_only_holdout"),
    ("empty_limitations", CORE + "test_ui_invalid_report_refused[empty_limitations]"),
    ("observation_floor", CORE + "test_significance_floor_is_mandatory[returns]"),
    ("trade_floor", CORE + "test_significance_floor_is_mandatory[round_trips]"),
    ("observed_drawdown_as_p95", CORE + "test_independent_bootstrap_not_observed_drawdown"),
    ("wrong_schema", CORE + "test_actual_ui_reader_accepts_publisher_output"),
    ("nonfinite_metric_guard", CORE + "test_ui_invalid_report_refused[nan]"),
    ("producer_contract", CORE + "test_real_history_producer_is_accepted_without_relabelling"),
    ("prior_holdout_peek", CORE + "test_known_prior_holdout_peek_refuses"),
    ("atomic_replace", CORE + "test_atomic_failure_preserves_previous_report"),
    ("partial_write", GUARDS + "test_short_write_cannot_replace_valid_report"),
    ("duplicate_keys", GUARDS + "test_duplicate_keys_refuse"),
    ("publisher_lock", GUARDS + "test_second_publisher_cannot_take_the_same_register_lock"),
    ("paper_only", CORE + "test_paper_only_refuses"),
    ("output_alias", GUARDS + "test_invalid_input_refuses_before_any_publisher_call[output]"),
    ("adjusted_prices", GUARDS + "test_invalid_input_refuses_before_any_publisher_call[adjusted]"),
    ("register_drift", GUARDS + "test_register_change_during_evaluation_refuses"),
]
BYPASS = {
    "empty_limitations": ("validate_report", "research_limitations_empty"),
    "observation_floor": ("_assert_evidence_floor", "research_observations_below_minimum"),
    "trade_floor": ("_assert_evidence_floor", "research_round_trips_below_minimum"),
    "nonfinite_metric_guard": ("validate_report", "research_ui_metric_invalid"),
    "prior_holdout_peek": ("_assert_unseen", "research_holdout_previously_observed"),
    "partial_write": ("_atomic_write", "research_partial_write"),
    "duplicate_keys": ("_unique_pairs", "research_duplicate_json_key"),
    "paper_only": ("_publish_research", "research_requires_paper_only"),
    "output_alias": ("publish_from_args", "research_output_overwrites_dataset"),
    "adjusted_prices": ("publish_from_args", "research_raw_daily_price_provenance_required"),
    "register_drift": ("_publish_research", "research_trial_register_changed_during_publication"),
}


def _reason(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(str(v.value) for v in node.values if isinstance(v, ast.Constant))
    return ""


def mutate(source, name):
    tree = ast.parse(source)
    changed = 0
    if name in BYPASS:
        function, reason = BYPASS[name]
        target = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == function)
        for node in ast.walk(target):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "_require" and len(node.args) == 2
                    and _reason(node.args[1]).startswith(reason)):
                node.args[0] = ast.Constant(True)
                changed += 1
    else:
        for node in ast.walk(tree):
            if name == "full_sample" and isinstance(node, ast.FunctionDef) and node.name == "_assert_split":
                node.body = [ast.Pass()]
                changed += 1
            elif (name == "wrong_schema" and isinstance(node, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == "SCHEMA" for t in node.targets)):
                node.value = ast.Constant("pramana.contest.v1")
                changed += 1
            elif (name == "observed_drawdown_as_p95" and isinstance(node, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == "p95" for t in node.targets)):
                node.value = ast.parse(
                    'maximum_drawdown(tuple(__import__("itertools").accumulate(returns, '
                    'lambda equity, value: equity * (1 + value), initial=Decimal(1))))',
                    mode="eval",
                ).body
                changed += 1
            elif (name == "producer_contract" and isinstance(node, ast.Compare)
                  and any(isinstance(c, ast.Name) and c.id == "PRICE_SERIES" for c in node.comparators)):
                node.comparators = [ast.Constant("raw_quote_unadjusted_close")]
                changed += 1
            elif (name == "publisher_lock" and isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Attribute) and node.func.attr == "flock"):
                node.func = ast.Name(id="tuple", ctx=ast.Load())
                node.args = []
                changed += 1
            elif (name == "atomic_replace" and isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Attribute) and node.func.attr == "replace"
                  and isinstance(node.func.value, ast.Name) and node.func.value.id == "os"):
                node.func = ast.Attribute(value=ast.Name(id="path", ctx=ast.Load()),
                                          attr="write_bytes", ctx=ast.Load())
                node.args = [ast.Name(id="document", ctx=ast.Load())]
                changed += 1
        if name == "trial_count_reset":
            class ResetCount(ast.NodeTransformer):
                def visit_Subscript(self, node):
                    nonlocal changed
                    if (isinstance(node.value, ast.Name) and node.value.id == "trials"
                            and isinstance(node.slice, ast.Constant) and node.slice.value == "candidate_trials"):
                        changed += 1
                        return ast.copy_location(ast.Constant(1), node)
                    return self.generic_visit(node)
            tree = ResetCount().visit(tree)
    assert changed > 0, f"sabotage no longer matches source: {name}"
    result = ast.unparse(ast.fix_missing_locations(tree)) + "\n"
    compile(result, str(MODULE), "exec")
    return result


@pytest.mark.parametrize("name,nodeid", CASES, ids=[name for name, _ in CASES])
def test_sabotage_is_caught_by_a_passing_regression(tmp_path, name, nodeid):
    original = (ROOT / MODULE).read_bytes()
    original_hash = hashlib.sha256(original).hexdigest()
    isolated = tmp_path / "isolated"
    shutil.copytree(ROOT / "src", isolated / "src",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (isolated / "tests").mkdir()
    for file in ("test_research_publisher.py", "test_research_publisher_guards.py"):
        shutil.copy2(ROOT / "tests" / file, isolated / "tests" / file)
    reader = Path("apps/pramana-ui/lib/research.ts")
    (isolated / reader).parent.mkdir(parents=True)
    shutil.copy2(ROOT / reader, isolated / reader)
    environment = {**os.environ, "PYTHONPATH": str(isolated / "src"),
                   "PYTHONDONTWRITEBYTECODE": "1", "TRADING_LIVE_MONEY_ACTIVE": "false"}
    environment.pop("PYTEST_ADDOPTS", None)

    def execute(label):
        evidence = tmp_path / f"{label}.xml"
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", nodeid, f"--junitxml={evidence}"],
            cwd=isolated, env=environment, capture_output=True, text=True, timeout=120,
        )
        (tmp_path / f"{label}.log").write_text(result.stdout + result.stderr)
        assert evidence.exists(), result.stdout + result.stderr
        root = ET.parse(evidence).getroot()
        suites = list(root.iter("testsuite"))
        errors = sum(int(s.get("errors", "0")) for s in suites)
        failures = sum(int(s.get("failures", "0")) for s in suites)
        assert errors == 0, result.stdout + result.stderr
        return result, failures

    try:
        control, failures = execute("control")
        assert control.returncode == 0 and failures == 0, control.stdout + control.stderr
        (isolated / MODULE).write_text(mutate(original.decode(), name))
        mutant, failures = execute("mutant")
        assert mutant.returncode == 1 and failures > 0, (
            f"SURVIVING/INVALID MUTANT {name}: {nodeid}\n" + mutant.stdout + mutant.stderr
        )
    finally:
        (isolated / MODULE).write_bytes(original)
        assert hashlib.sha256((ROOT / MODULE).read_bytes()).hexdigest() == original_hash
        assert (isolated / MODULE).read_bytes() == original
