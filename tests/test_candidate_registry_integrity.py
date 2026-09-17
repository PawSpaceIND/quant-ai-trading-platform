"""Synthetic candidate governance: no trainer, broker, model deployment or live grants."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from test_ai_learning_governance import NOW, H, evaluation, strategy

from quant_ai.learning import candidates as registry
from quant_ai.learning.candidates import CandidateStage as Stage
from quant_ai.operations.evidence_log import append_record, read_records, verify_chain


def staged(path):
    for index, stage in enumerate((Stage.SHADOW, Stage.PAPER_CANDIDATE)):
        registry.transition_candidate(path, candidate_id="candidate-1", target=stage,
                                      evidence_sha256=H, now=NOW + timedelta(seconds=index))


def approval_args(**changes):
    value = evaluation()
    evidence = strategy()
    args = {"candidate_id": value.candidate_id, "target": Stage.PAPER_APPROVED,
            "evidence_sha256": registry.candidate_assessment_sha256(value, evidence),
            "reviewer": "synthetic-reviewer", "evaluation": value,
            "strategy_evidence": evidence, "now": NOW + timedelta(seconds=2)}
    args.update(changes)
    return args


def test_paper_approval_requires_actual_passing_assessment(tmp_path):
    path = tmp_path / "candidates.jsonl"
    staged(path)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="candidate_approval_assessment_required"):
        registry.transition_candidate(path, candidate_id="candidate-1", target=Stage.PAPER_APPROVED,
                                      evidence_sha256=H, reviewer="synthetic-reviewer", now=NOW)
    assert path.read_bytes() == before


@pytest.mark.parametrize("field,value", [
    ("from_stage", "PAPER_APPROVED"), ("stage", "PAPER_APPROVED"),
    ("live_execution_authorized", True), ("live_execution_authorized", "false"),
    ("evidence_sha256", "not-a-digest"), ("candidate_id", 123),
    ("candidate_id", "bad\nidentity"), ("reviewer", 42),
])
def test_hash_valid_but_semantically_invalid_history_is_refused(tmp_path, field, value):
    path = tmp_path / "invalid.jsonl"
    payload = {"schema": "pramana.ai_candidate_registry.v1", "candidate_id": "candidate-1",
               "from_stage": "RESEARCH", "stage": "SHADOW", "evidence_sha256": H,
               "reviewer": None, "live_execution_authorized": False}
    payload[field] = value
    append_record(path, registry.EVENT, payload, now=NOW)
    assert verify_chain(read_records(path))
    before = path.read_bytes()
    with pytest.raises((ValueError, TypeError)):
        registry.candidate_stages(path)
    assert path.read_bytes() == before


def test_well_formed_legacy_paper_approval_is_not_new_qualified_evidence(tmp_path):
    path = tmp_path / "legacy.jsonl"
    prior = Stage.RESEARCH
    for stage in (Stage.SHADOW, Stage.PAPER_CANDIDATE, Stage.PAPER_APPROVED):
        append_record(path, registry.EVENT, {
            "schema": "pramana.ai_candidate_registry.v1", "candidate_id": "candidate-1",
            "from_stage": prior.value, "stage": stage.value, "evidence_sha256": H,
            "reviewer": "legacy-reviewer", "live_execution_authorized": False}, now=NOW)
        prior = stage
    before = path.read_bytes()
    with pytest.raises(ValueError, match="legacy_approval_requires_review"):
        registry.candidate_stages(path)
    assert path.read_bytes() == before


def test_backward_registry_time_refuses_without_appending(tmp_path):
    path = tmp_path / "time.jsonl"
    registry.transition_candidate(path, candidate_id="candidate-1", target=Stage.SHADOW,
                                  evidence_sha256=H, now=NOW)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="candidate_registry_time_regressed"):
        registry.transition_candidate(path, candidate_id="candidate-1", target=Stage.PAPER_CANDIDATE,
                                      evidence_sha256=H, now=NOW - timedelta(seconds=1))
    assert path.read_bytes() == before


@pytest.mark.parametrize("field,value", [("after_cost_expectancy", Decimal("Infinity")),
    ("after_cost_expectancy", Decimal("NaN")), ("resolved_probability_count", True),
    ("resolved_probability_count", Decimal("100.5"))])
def test_invalid_evaluation_is_refused_before_assessment(field, value):
    with pytest.raises((ValueError, TypeError), match="candidate_evaluation"):
        registry.assess_candidate(replace(evaluation(), **{field: value}), strategy())


def test_replayed_history_validates_from_stage_not_only_latest_state(tmp_path):
    path = tmp_path / "from.jsonl"
    staged(path)
    append_record(path, registry.EVENT, {
        "schema": "pramana.ai_candidate_registry.v1", "candidate_id": "candidate-1",
        "from_stage": "RESEARCH", "stage": "SHADOW", "evidence_sha256": H,
        "reviewer": None, "live_execution_authorized": False}, now=NOW + timedelta(seconds=2))
    with pytest.raises(ValueError, match="candidate_registry_transition_invalid"):
        registry.candidate_stages(path)


def test_approval_round_trip_binds_evaluation_and_replays_without_live_authority(tmp_path):
    path = tmp_path / "approved.jsonl"
    staged(path)
    args = approval_args()
    record = registry.transition_candidate(path, **args)
    assert record["payload"]["assessment"] is not None
    assert record["payload"]["evidence_sha256"] == args["evidence_sha256"]
    assert record["payload"]["live_execution_authorized"] is False
    before = path.read_bytes()
    assert registry.candidate_stages(path)["candidate-1"] is Stage.PAPER_APPROVED
    assert path.read_bytes() == before
    registry.transition_candidate(path, candidate_id="candidate-1", target=Stage.SHADOW,
                                  evidence_sha256=H, now=NOW + timedelta(seconds=3))
    assert registry.candidate_stages(path)["candidate-1"] is Stage.SHADOW
    assert verify_chain(read_records(path))


@pytest.mark.parametrize("defect", ["candidate", "hash", "failed_probability", "failed_strategy"])
def test_approval_evidence_mismatch_or_failed_assessment_never_appends(tmp_path, defect):
    path = tmp_path / "evidence.jsonl"
    staged(path)
    args = approval_args()
    if defect == "candidate":
        args["evaluation"] = replace(args["evaluation"], candidate_id="another-candidate")
    elif defect == "hash":
        args["evidence_sha256"] = "b" * 64
    elif defect == "failed_probability":
        args["evaluation"] = evaluation(brier="0.4")
        args["evidence_sha256"] = registry.candidate_assessment_sha256(args["evaluation"], args["strategy_evidence"])
    else:
        args["strategy_evidence"] = replace(args["strategy_evidence"], paper_days=0)
        args["evidence_sha256"] = registry.candidate_assessment_sha256(args["evaluation"], args["strategy_evidence"])
    before = path.read_bytes()
    with pytest.raises(ValueError, match="candidate_approval"):
        registry.transition_candidate(path, **args)
    assert path.read_bytes() == before


@pytest.mark.parametrize("field,value", [("target", "SHADOW"), ("target", True),
    ("candidate_id", "bad\nidentity"), ("candidate_id", " leading"), ("candidate_id", "x" * 257),
    ("reviewer", "bad\u2028reviewer")])
def test_invalid_transition_input_does_not_create_registry(tmp_path, field, value):
    path = tmp_path / "bad-input.jsonl"
    args = {"candidate_id": "candidate-1", "target": Stage.SHADOW,
            "evidence_sha256": H, "now": NOW}
    args[field] = value
    with pytest.raises((TypeError, ValueError), match="candidate_"):
        registry.transition_candidate(path, **args)
    assert not path.exists()


def test_legacy_nonapproval_history_is_preserved_and_can_progress(tmp_path):
    path = tmp_path / "legacy-research.jsonl"
    append_record(path, registry.EVENT, {"schema": "pramana.ai_candidate_registry.v1",
        "candidate_id": "candidate-1", "from_stage": "RESEARCH", "stage": "SHADOW",
        "evidence_sha256": H, "reviewer": None, "live_execution_authorized": False}, now=NOW)
    prefix = path.read_bytes()
    registry.transition_candidate(path, candidate_id="candidate-1", target=Stage.PAPER_CANDIDATE,
                                  evidence_sha256=H, now=NOW + timedelta(seconds=1))
    assert path.read_bytes().startswith(prefix)
    assert registry.candidate_stages(path)["candidate-1"] is Stage.PAPER_CANDIDATE


@pytest.mark.parametrize("defect", ["negative_skill", "candidate", "no_reviewer", "missing_assessment", "boolean_coercion"])
def test_replay_recomputes_even_correctly_rehashed_approval_payload(tmp_path, defect):
    import copy
    import hashlib
    import json
    valid = tmp_path / "valid.jsonl"
    staged(valid)
    record = registry.transition_candidate(valid, **approval_args())
    payload = copy.deepcopy(record["payload"])
    if defect == "negative_skill":
        payload["assessment"]["evaluation"]["brier_score"] = "0.4"
    elif defect == "candidate":
        payload["assessment"]["evaluation"]["candidate_id"] = "other"
    elif defect == "no_reviewer":
        payload["reviewer"] = None
    elif defect == "missing_assessment":
        payload["assessment"] = None
    else:
        payload["assessment"]["paper_ready"] = 1
    payload["evidence_sha256"] = hashlib.sha256(json.dumps(payload["assessment"],
        sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
    broken = tmp_path / "broken.jsonl"
    staged(broken)
    append_record(broken, registry.EVENT, payload, now=NOW + timedelta(seconds=2))
    assert verify_chain(read_records(broken))
    with pytest.raises((TypeError, ValueError)):
        registry.candidate_stages(broken)


@pytest.mark.parametrize("field,value", [("sample_trades", True), ("paper_days", 100.5),
    ("expectancy", Decimal("Infinity")), ("max_drawdown", Decimal("NaN")),
    ("profit_factor", Decimal(-1))])
def test_malformed_strategy_metrics_cannot_qualify_candidate(field, value):
    with pytest.raises((TypeError, ValueError), match="candidate_"):
        registry.assess_candidate(evaluation(), replace(strategy(), **{field: value}))


def test_assessment_digest_changes_with_policy_and_evidence_contract():
    from quant_ai.validation.promotion import PromotionPolicy
    value, evidence = evaluation(), strategy()
    original = registry.candidate_assessment_sha256(value, evidence)
    assert original == registry.candidate_assessment_sha256(value, evidence)
    assert original != registry.candidate_assessment_sha256(replace(value, holdout_sha256="b"*64), evidence)
    assert original != registry.candidate_assessment_sha256(value, replace(evidence, paper_days=36))
    assert original != registry.candidate_assessment_sha256(value, evidence, PromotionPolicy(min_paper_days=31))


def test_missing_registry_is_read_only_and_private_file_is_created_on_write(tmp_path):
    path = tmp_path / "missing.jsonl"
    assert registry.candidate_stages(path) == {}
    assert not path.exists()
    staged(path)
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_registry_aliases_refuse_without_modifying_original(tmp_path, kind):
    import os
    original = tmp_path / "original.jsonl"
    staged(original)
    alias = tmp_path / "alias.jsonl"
    if kind == "symlink":
        alias.symlink_to(original)
    else:
        os.link(original, alias)
    before = original.read_bytes()
    with pytest.raises(ValueError, match="candidate_registry_"):
        registry.transition_candidate(alias, **approval_args())
    assert original.read_bytes() == before


def test_competing_process_cannot_read_partial_state_or_append_second_transition(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    path = tmp_path / "race.jsonl"
    staged(path)
    code = """
import fcntl, sys
with open(sys.argv[1], 'r+') as handle:
    fcntl.flock(handle, fcntl.LOCK_EX)
    print('LOCKED', flush=True)
    sys.stdin.readline()
"""
    process = subprocess.Popen([sys.executable, "-c", code, str(path)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "PYTHONPATH": str(Path.cwd() / "src")})
    try:
        assert process.stdout.readline().strip() == "LOCKED"
        before = path.read_bytes()
        with pytest.raises(ValueError, match="candidate_registry_busy"):
            registry.transition_candidate(path, **approval_args())
        with pytest.raises(ValueError, match="candidate_registry_busy"):
            registry.candidate_stages(path)
        assert path.read_bytes() == before
    finally:
        process.communicate("release\n", timeout=5)
    registry.transition_candidate(path, **approval_args())
    assert registry.candidate_stages(path)["candidate-1"] is Stage.PAPER_APPROVED


def test_competing_thread_cannot_append_from_the_same_previous_stage(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    path = tmp_path / "threads.jsonl"
    entered, release = Event(), Event()
    original = registry.append_record
    def slow_append(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(registry, "append_record", slow_append)
    args = {"candidate_id": "candidate-1", "target": Stage.SHADOW,
            "evidence_sha256": H, "now": NOW}
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(registry.transition_candidate, path, **args)
        try:
            assert entered.wait(5)
            with pytest.raises(ValueError, match="candidate_registry_busy"):
                registry.transition_candidate(path, **args)
        finally:
            release.set()
        task.result(timeout=5)
    assert len(read_records(path)) == 1
    assert registry.candidate_stages(path)["candidate-1"] is Stage.SHADOW


def test_interrupted_append_cannot_be_treated_as_valid_history(tmp_path):
    path = tmp_path / "truncated.jsonl"
    staged(path)
    with path.open("ab") as handle:
        handle.write(b'{"schema":"pramana.evidence_log.v1"')
    before = path.read_bytes()
    with pytest.raises(ValueError):
        registry.candidate_stages(path)
    with pytest.raises(ValueError):
        registry.transition_candidate(path, **approval_args())
    assert path.read_bytes() == before


def test_record_without_final_newline_is_not_extended_into_invalid_json(tmp_path):
    path = tmp_path / "missing-newline.jsonl"
    staged(path)
    path.write_bytes(path.read_bytes().rstrip(b"\n"))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="candidate_registry_incomplete_record"):
        registry.transition_candidate(path, **approval_args())
    assert path.read_bytes() == before


def test_duplicate_json_keys_in_hash_valid_history_refuse(tmp_path):
    path = tmp_path / "duplicates.jsonl"
    staged(path)
    text = path.read_text().replace('"candidate_id":"candidate-1"',
        '"candidate_id":"hidden","candidate_id":"candidate-1"')
    path.write_text(text)
    assert verify_chain(read_records(path))
    with pytest.raises(ValueError, match="candidate_registry_duplicate_json_key"):
        registry.candidate_stages(path)



def test_abrupt_exit_after_record_commit_is_replayed_without_second_approval(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    path = tmp_path / "crash.jsonl"
    code = """
import os, sys
from test_candidate_registry_integrity import staged, approval_args
from quant_ai.learning import candidates as registry
staged(sys.argv[1])
original = registry.append_record
def crash(*args, **kwargs):
    original(*args, **kwargs)
    os._exit(73)
registry.append_record = crash
registry.transition_candidate(sys.argv[1], **approval_args())
"""
    result = subprocess.run([sys.executable, "-c", code, str(path)], check=False, timeout=20,
        capture_output=True, text=True, env={**os.environ, "PYTHONPATH": os.pathsep.join(
            (str(Path.cwd() / "src"), str(Path.cwd() / "tests")))})
    assert result.returncode == 73, result.stdout + result.stderr
    assert registry.candidate_stages(path)["candidate-1"] is Stage.PAPER_APPROVED
    before = path.read_bytes()
    with pytest.raises(ValueError, match="invalid_candidate_transition"):
        registry.transition_candidate(path, **approval_args())
    assert path.read_bytes() == before
    assert len(read_records(path)) == 3


def test_invalid_replay_prevents_any_further_append(tmp_path):
    path = tmp_path / "corrupt.jsonl"
    staged(path)
    append_record(path, registry.EVENT, {"schema": "pramana.ai_candidate_registry.v1",
        "candidate_id": "candidate-1", "from_stage": "SHADOW", "stage": "RESEARCH",
        "evidence_sha256": H, "reviewer": None, "live_execution_authorized": False},
        now=NOW + timedelta(seconds=2))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="candidate_registry_transition_invalid"):
        registry.transition_candidate(path, candidate_id="candidate-2", target=Stage.SHADOW,
                                      evidence_sha256=H, now=NOW + timedelta(seconds=3))
    assert path.read_bytes() == before
