"""Use the existing offline copied-source mutation runner unchanged."""
import hashlib
import json
from pathlib import Path

import pytest
import test_required_history_warmup_mutations as harness

ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads((ROOT / "docs/evidence/session-health-guards.json").read_text())["mutations"]
PROTECTED = {c["path"] for c in CASES} | {c["test"].split("::")[0] for c in CASES}


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_session_observation_guard_has_passing_control_and_failing_mutant(tmp_path, case):
    before = {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in PROTECTED}
    try:
        harness.test_required_warmup_guard_requires_passing_control_and_failing_mutant(tmp_path, case)
    finally:
        assert {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in PROTECTED} == before
