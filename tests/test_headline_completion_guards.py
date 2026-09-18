"""Reuse the original paired guard runner without editing any prior test or assertion."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import test_consensus_output_guards as paired

ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads((ROOT / "docs/evidence/headline-completion-guards.json").read_text())["mutations"]
PROTECTED = {case["path"] for case in CASES} | {case["test"].split("::")[0] for case in CASES}


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_headline_guard_has_passing_control_and_detected_change(tmp_path, case):
    before = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in PROTECTED}
    try:
        paired.test_consensus_guard_has_passing_control_and_detected_change(tmp_path, case)
    finally:
        after = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in PROTECTED}
        assert before == after
