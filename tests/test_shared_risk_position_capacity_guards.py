"""Paired controls and deliberate removals for position-linked shared-risk capacity."""
from __future__ import annotations

import ast
import hashlib
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from test_kite_history_mutations import (
    test_kite_history_guard_requires_passing_control_and_failing_mutant as run_guard,
)

ROOT = Path(__file__).resolve().parents[1]
BINDING = "src/quant_ai/execution/shared_risk_binding.py"
SHARED = "src/quant_ai/execution/shared_risk.py"
INSTITUTIONAL = "src/quant_ai/execution/institutional.py"
ALL_BEHAVIOR = "tests/test_shared_risk_position_capacity.py"
PREPARE_OVERRIDE = ALL_BEHAVIOR + "::test_prepare_refuses_impossible_effective_capacity_override"
DISPATCH_OVERRIDE = ALL_BEHAVIOR + "::test_dispatch_refuses_impossible_effective_capacity_override"
FULL_CLOSE = ALL_BEHAVIOR + "::test_reconciled_full_close_reopens_shared_account_capacity"


def helper_guard_cases():
    source = (ROOT / BINDING).read_text()
    tree = ast.parse(source)
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "position_linked_capacity"
    )
    result = []
    seen = {}
    for node in ast.walk(function):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_check"
        ):
            continue
        reason = node.args[1].value
        seen[reason] = seen.get(reason, 0) + 1
        result.append(
            {
                "id": f"helper_{reason}_{seen[reason]}",
                "path": BINDING,
                "old": ast.get_source_segment(source, node),
                "new": "None",
                "test": ALL_BEHAVIOR,
            }
        )
    return result


CASES = helper_guard_cases() + [
    {
        "id": "reserve_effective_not_above_raw",
        "path": SHARED,
        "old": '_check(existing <= raw_existing, "position_capacity_exceeds_reservations")',
        "new": "None",
        "test": PREPARE_OVERRIDE,
    },
    {
        "id": "dispatch_effective_not_above_raw",
        "path": SHARED,
        "old": '_check(effective <= raw_reserved, "position_capacity_exceeds_reservations")',
        "new": "None",
        "test": DISPATCH_OVERRIDE,
    },
    {
        "id": "prepare_uses_effective_existing",
        "path": INSTITUTIONAL,
        "old": "existing_reserved_loss=effective_existing,",
        "new": "existing_reserved_loss=None,",
        "test": FULL_CLOSE,
    },
    {
        "id": "dispatch_uses_effective_reserved",
        "path": INSTITUTIONAL,
        "old": "effective_reserved_loss=effective_reserved,",
        "new": "effective_reserved_loss=None,",
        "test": FULL_CLOSE,
    },
]


SNAPSHOT_TEST = "tests/test_position_capacity_snapshot.py::"
CASES += [
    {"id": "ledger_snapshot", "path": BINDING,
     "old": "with _capacity_read_snapshot(ledger), _capacity_read_snapshot(journal):",
     "new": "with _capacity_read_snapshot(journal):",
     "test": SNAPSHOT_TEST + "test_external_position_change_after_reconcile_does_not_free_unchecked_capacity"},
    {"id": "journal_snapshot", "path": BINDING,
     "old": "with _capacity_read_snapshot(ledger), _capacity_read_snapshot(journal):",
     "new": "with _capacity_read_snapshot(ledger):",
     "test": SNAPSHOT_TEST + "test_external_slice_change_after_binding_is_not_adopted_in_capacity"},
    {"id": "conservative_lot_attribution", "path": BINDING,
     "old": "candidates, key=lambda item: (item[0], item[2]), reverse=True",
     "new": "candidates, key=lambda item: (item[0], item[2]), reverse=False",
     "test": SNAPSHOT_TEST + "test_remaining_ambiguous_shares_keep_highest_parent_risk_first"},
]


@pytest.mark.parametrize("case", CASES, ids=[row["id"] for row in CASES])
def test_position_capacity_guard_needs_control_and_named_failure(tmp_path, case):
    protected = {
        case["path"],
        "tests/test_shared_risk_position_capacity.py",
        BINDING,
        SHARED,
        INSTITUTIONAL,
    }
    before = {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        for name in protected
    }
    run_guard(tmp_path, case)
    failures = [
        (row.get("name"), failure.get("message", ""))
        for row in ET.parse(tmp_path / "mutant.xml").iter("testcase")
        for failure in row.findall("failure")
    ]
    assert failures
    assert all(name and message.startswith(("assert ", "AssertionError:", "Failed:"))
               for name, message in failures), failures
    assert before == {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        for name in protected
    }
