"""Paired copied-source guards exercising the persisted health observer."""
import xml.etree.ElementTree as ET

import pytest
from test_kite_history_mutations import (
    test_kite_history_guard_requires_passing_control_and_failing_mutant as run_guard,
)
from test_mcx_seasonal_guards import CASES


@pytest.mark.parametrize("case", CASES, ids=[row["id"] for row in CASES])
def test_mcx_health_boundaries_detect_removed_calendar_guards(tmp_path, case):
    selection = {
        "standard_close": "nse-stale-before-winter",
        "dst_close": "nse-stale-at-summer",
        "exclusive_close": "nse-stale-at-summer",
    }
    target = "tests/test_mcx_health_session_boundaries.py::"
    target += "test_persisted_health_honors_mcx_exclusive_close[" + selection[case["id"]] + "]"
    run_guard(tmp_path, {**case, "test": target})
    failures = [item.get("message", "")
                for item in ET.parse(tmp_path / "mutant.xml").iter("failure")]
    assert failures and all(message.startswith(("assert ", "AssertionError:"))
                            for message in failures), failures
