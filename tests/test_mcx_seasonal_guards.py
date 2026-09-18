"""Paired regression controls for the corrected normal MCX close boundaries."""
import xml.etree.ElementTree as ET

import pytest
from test_kite_history_mutations import (
    test_kite_history_guard_requires_passing_control_and_failing_mutant as run_guard,
)

PATH = "src/quant_ai/execution/session.py"
TARGET = "tests/test_mcx_seasonal_close.py::"
CASES = [
    {"id": "standard_close", "path": PATH,
     "old": '"Asia/Kolkata", time(8, 45), time(9), time(23, 55), time(23, 55),',
     "new": '"Asia/Kolkata", time(8, 45), time(9), time(23, 30), time(23, 30),',
     "test": TARGET + "test_mcx_regular_and_post_close_are_the_same_in_both_seasons[day0-close0]"},
    {"id": "dst_close", "path": PATH,
     "old": "us_dst_regular_close=time(23, 30), us_dst_post_close=time(23, 30),",
     "new": "us_dst_regular_close=time(23, 55), us_dst_post_close=time(23, 55),",
     "test": TARGET + "test_mcx_regular_and_post_close_are_the_same_in_both_seasons[day1-close1]"},
    {"id": "exclusive_close", "path": PATH,
     "old": "if session.regular_open <= local_time < regular_close:",
     "new": "if session.regular_open <= local_time <= regular_close:",
     "test": TARGET + "test_published_mcx_normal_close_has_an_exact_exclusive_boundary[0-CLOSED-day1-close1]"},
]


@pytest.mark.parametrize("case", CASES, ids=[row["id"] for row in CASES])
def test_mcx_seasonal_protection_needs_passing_control_and_failing_assertion(tmp_path, case):
    run_guard(tmp_path, case)
    failures = [item.get("message", "")
                for item in ET.parse(tmp_path / "mutant.xml").iter("failure")]
    assert failures and all(message.startswith(("assert ", "AssertionError:"))
                            for message in failures), failures
