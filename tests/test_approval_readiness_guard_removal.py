"""Reuse the existing offline copied-source guard-removal harness."""
import hashlib
import json
from pathlib import Path

import pytest
import test_required_history_warmup_mutations as harness

ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads((ROOT / "docs/evidence/approval-readiness-guards.json").read_text())["mutations"]
PROTECTED = tuple(sorted({c["path"] for c in CASES} | {c["test"].split("::")[0] for c in CASES}))


def hashes():
    return {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in PROTECTED}


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_approval_guard_removal_is_detected_without_changing_source(tmp_path, case):
    before = hashes()
    try:
        harness.test_required_warmup_guard_requires_passing_control_and_failing_mutant(tmp_path, case)
    finally:
        assert hashes() == before
