"""Each isolated mutation must fail a named passing behavioral regression.

The interpreter recompiles an in-memory module; working source is never edited.
These are offline software checks, not live trading/risk authorization.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

F = "quant_ai.analytics.feedback"
A = "quant_ai.analytics.attribution"
M = "quant_ai.analytics.learning_monitor"
FB = "tests/test_specialist_feedback.py::"
MON = "tests/test_learning_monitor.py::"
CASES = [
    ("supporter_only", F, [("target = supporters if vote[\"stance\"] in UP and confidence > 0 else ignored", "target = supporters")],
     FB + "test_only_supporters_receive_realized_entry_credit"),
    ("closed_entry_scope", F, [("if row.get(\"governance\") != \"filled\" or row.get(\"side\") != \"BUY\" or row.get(\"stance\") not in UP:", "if False:")],
     FB + "test_invalid_or_ineligible_outcomes_never_change_weights"),
    ("outcome_time", F, [("if exited < decided or exited > now:", "if False:")],
     FB + "test_invalid_or_ineligible_outcomes_never_change_weights"),
    ("source_consistency", F, [("if incoming.get(old[0]) != raw:", "if False:")],
     FB + "test_deleted_consumed_source_does_not_silently_reset_history"),
    ("audit_digest", F, [("if hashlib.sha256(raw.encode()).hexdigest() != digest:", "if False:")],
     FB + "test_corrupt_audit_digest_refuses_even_when_source_payload_is_unchanged"),
    ("audit_immutability", F, [("for verb in (\"UPDATE\", \"DELETE\"):", "for verb in ():")],
     FB + "test_feedback_audit_is_append_only"),
    ("unrelated_trace_credit", A, [("        if self._journal_binding is not None:\n            self._refresh_bound()\n            return", "        if False:\n            self._refresh_bound()\n            return")],
     FB + "test_bound_legacy_delta_callback_cannot_credit_the_latest_unrelated_trace"),
    ("next_decision_refresh", A, [("        self._refresh_bound(now)\n        basis =", "        basis =")],
     FB + "test_new_resolved_outcome_is_used_before_next_evidence_weighting"),
    ("monitor_sample_floor", M, [("if len(reference) < MIN_SAMPLES or len(current) < MIN_SAMPLES:", "if False:"),
                                  ("min_samples=MIN_SAMPLES", "min_samples=1")],
     MON + "test_minimum_window_sample_is_not_lowered"),
    ("monitor_nonoverlap", M, [("if previous is not None and row[\"_decision\"] < previous:", "if False:")],
     MON + "test_overlapping_windows_cannot_multiply_eligible_samples"),
    ("monitor_recency", M, [("if now - max(row[\"_resolved\"] for row in current) > timedelta(seconds=config[\"maximum_recent_age_seconds\"]):", "if False:")],
     MON + "test_stale_recent_evidence_is_not_healthy"),
    ("monitor_tenant", M, [("if data[\"tenant_id\"] != tenant:", "if False:")],
     MON + "test_invalid_configuration_and_mixed_artifacts_refuse"),
]

CHILD = r"""
import importlib, inspect, json, sys
import pytest
module_name, replacements, target, junit, base = sys.argv[1:]
module = importlib.import_module(module_name)
source = inspect.getsource(module)
for old, new in json.loads(replacements):
    if old not in source:
        raise RuntimeError('mutation_anchor_missing')
    source = source.replace(old, new)
if json.loads(replacements):
    exec(compile(source, module.__file__, 'exec'), module.__dict__)
sys.exit(pytest.main(['-q', target, '--tb=short', '--junitxml='+junit, '--basetemp='+base]))
"""


def totals(path):
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root)
    return {key: sum(int(suite.get(key, 0)) for suite in suites)
            for key in ("tests", "failures", "errors", "skipped")}


@pytest.mark.parametrize("name,module,replacements,target", CASES, ids=[row[0] for row in CASES])
def test_learning_guard_sensitivity(tmp_path, name, module, replacements, target):
    root = Path(__file__).resolve().parents[1]
    home = tmp_path / "home"; home.mkdir()
    harness = tmp_path / "harness"; harness.mkdir()
    (harness / "sitecustomize.py").write_text(
        "import sys\ndef deny(event,args):\n"
        "    if event in ('socket.connect','socket.connect_ex','socket.getaddrinfo','socket.sendto'):\n"
        "        raise RuntimeError('offline_learning_guard_network_refused')\n"
        "sys.addaudithook(deny)\n"
    )
    env = {"HOME": str(home), "PATH": os.pathsep.join((str(Path(sys.executable).parent), "/usr/bin", "/bin")),
           "PYTHONPATH": os.pathsep.join((str(harness), str(root / "src"), str(root / "tests"))),
           "PYTHONDONTWRITEBYTECODE": "1", "TRADING_LIVE_MONEY_ACTIVE": "false"}
    results = []
    for label, changes in (("control", []), ("mutant", replacements)):
        junit = tmp_path / (label + ".xml")
        result = subprocess.run(
            [sys.executable, "-B", "-c", CHILD, module, json.dumps(changes), target,
             str(junit), str(tmp_path / label)], cwd=root, env=env,
            capture_output=True, text=True, timeout=60, check=False,
        )
        (tmp_path / (label + ".log")).write_text(result.stdout + result.stderr)
        counts = totals(junit)
        assert counts["tests"] > 0 and counts["errors"] == counts["skipped"] == 0, result.stdout + result.stderr
        if label == "control":
            assert result.returncode == 0 and counts["failures"] == 0, result.stdout + result.stderr
        else:
            assert result.returncode == 1 and counts["failures"] > 0, result.stdout + result.stderr
        results.append({"label": label, "exit": result.returncode, **counts})
    (tmp_path / "sensitivity.json").write_text(json.dumps({"guard": name, "target": target, "runs": results}))
