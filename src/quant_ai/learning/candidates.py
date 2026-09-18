"""Champion/challenger governance for AI candidates in shadow and paper modes only."""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path

from quant_ai.learning.contracts import CandidateEvaluation
from quant_ai.operations.evidence_log import append_record, verify_chain
from quant_ai.validation.promotion import (
    PromotionDecision,
    PromotionPolicy,
    SelectionEvidence,
    StrategyEvidence,
    evaluate_promotion,
)

_SHA256 = re.compile(r"[0-9a-f]{64}")
EVENT = "ai_candidate_stage"
LEGACY_SCHEMA = "pramana.ai_candidate_registry.v1"
SCHEMA = "pramana.ai_candidate_registry.v2"
ASSESSMENT_SCHEMA = "pramana.ai_candidate_assessment.v1"
MAX_REGISTRY_BYTES = 16 * 1024 * 1024


class CandidateStage(str, Enum):
    RESEARCH = "RESEARCH"
    SHADOW = "SHADOW"
    PAPER_CANDIDATE = "PAPER_CANDIDATE"
    PAPER_APPROVED = "PAPER_APPROVED"


_ALLOWED = {
    CandidateStage.RESEARCH: {CandidateStage.SHADOW},
    CandidateStage.SHADOW: {CandidateStage.PAPER_CANDIDATE, CandidateStage.RESEARCH},
    CandidateStage.PAPER_CANDIDATE: {CandidateStage.PAPER_APPROVED, CandidateStage.SHADOW},
    CandidateStage.PAPER_APPROVED: {CandidateStage.SHADOW},
}


@dataclass(frozen=True)
class CandidateAssessment:
    shadow_ready: bool
    paper_ready: bool
    live_ready: bool
    reasons: tuple[str, ...]
    strategy_decision: PromotionDecision


def assess_candidate(
    evaluation: CandidateEvaluation,
    strategy_evidence: StrategyEvidence,
    policy: PromotionPolicy | None = None,
    *,
    selection: SelectionEvidence | None = None,
) -> CandidateAssessment:
    """Demand trading evidence, probability skill and a corrected search; never approve live money."""
    chosen = policy if policy is not None else PromotionPolicy()
    _validate_assessment_inputs(evaluation, strategy_evidence, chosen)
    strategy = evaluate_promotion(strategy_evidence, chosen, selection=selection)
    reasons = list(strategy.reasons)
    if evaluation.after_cost_expectancy <= 0:
        reasons.append("candidate_after_cost_expectancy_not_positive")
    if evaluation.max_drawdown > chosen.max_drawdown:
        reasons.append("candidate_evaluation_drawdown_too_high")
    if evaluation.resolved_probability_count < chosen.min_trades:
        reasons.append("candidate_probability_sample_too_small")
    if evaluation.brier_score >= evaluation.baseline_brier_score:
        reasons.append("candidate_probability_skill_not_better_than_baseline")
    paper_ready = not reasons
    return CandidateAssessment(
        shadow_ready=True,
        paper_ready=paper_ready,
        live_ready=False,
        reasons=tuple(dict.fromkeys(reasons)),
        strategy_decision=strategy,
    )



def _text(value, name, *, optional=False):
    if value is None and optional:
        return
    if (not isinstance(value, str) or not value or value != value.strip() or len(value) > 256
            or any(ord(char) < 32 or ord(char) == 127 or char in "\x85\u2028\u2029" for char in value)):
        raise ValueError(f"candidate_{name}_invalid")


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def _digest(value):
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError("candidate_evidence_sha256_invalid")


def _instant(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("candidate_registry_time_must_be_timezone_aware")
    return value.astimezone(timezone.utc)


def _validate_assessment_inputs(evaluation, evidence, policy):
    if not isinstance(evaluation, CandidateEvaluation):
        raise TypeError("candidate_evaluation_type_invalid")
    if not isinstance(evidence, StrategyEvidence) or not isinstance(policy, PromotionPolicy):
        raise TypeError("candidate_strategy_or_policy_type_invalid")
    _text(evaluation.candidate_id, "evaluation_identity")
    for obj, label, integer_fields in (
        (evaluation, "evaluation", {"resolved_probability_count"}),
        (evidence, "strategy", {"sample_trades", "profitable_regimes", "paper_days"}),
        (policy, "policy", {"min_trades", "min_profitable_regimes", "min_paper_days"}),
    ):
        for name in integer_fields:
            value = getattr(obj, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"candidate_{label}_{name}_invalid")
        for field in fields(obj):
            value = getattr(obj, field.name)
            # require_selection_correction is the one boolean on a policy otherwise made of
            # Decimal thresholds; it is checked for type below rather than as an amount.
            if (
                field.name in integer_fields
                or field.name.endswith("sha256")
                or field.name in {"candidate_id", "require_selection_correction"}
            ):
                continue
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError(f"candidate_{label}_{field.name}_invalid")
    if not 0 <= evaluation.brier_score <= 1 or not 0 <= evaluation.baseline_brier_score <= 1:
        raise ValueError("candidate_evaluation_brier_invalid")
    if any(not 0 <= value <= 1 for value in (evaluation.max_drawdown, evidence.max_drawdown, policy.max_drawdown)):
        raise ValueError("candidate_evaluation_drawdown_invalid")
    if evidence.profit_factor < 0 or policy.min_profit_factor < 0 or policy.min_trades < 1:
        raise ValueError("candidate_strategy_or_policy_invalid")
    if type(policy.require_selection_correction) is not bool:
        raise ValueError("candidate_policy_require_selection_correction_invalid")
    if not 0 <= policy.min_deflated_sharpe <= 1:
        raise ValueError("candidate_policy_min_deflated_sharpe_invalid")
    for field in fields(evaluation):
        if field.name.endswith("sha256"):
            _digest(getattr(evaluation, field.name))


def _serialize(obj):
    return {name: str(value) if isinstance(value, Decimal) else value for name, value in asdict(obj).items()}


def _assessment_payload(evaluation, strategy_evidence, policy=None, selection=None):
    chosen = policy if policy is not None else PromotionPolicy()
    result = assess_candidate(evaluation, strategy_evidence, chosen, selection=selection)
    return {
        "schema": ASSESSMENT_SCHEMA,
        "evaluation": _serialize(evaluation), "strategy_evidence": _serialize(strategy_evidence),
        "policy": _serialize(chosen),
        # The correction for how hard the search looked is part of the signed record, not a
        # side input: an approval has to carry the evidence it relied on, and a later reader
        # must be able to see that the search was counted rather than assume it.
        "selection": _serialize(selection) if selection is not None else None,
        "paper_ready": result.paper_ready,
        "live_ready": result.live_ready, "reasons": list(result.reasons),
        "strategy_approved": result.strategy_decision.approved,
    }


def candidate_assessment_sha256(
    evaluation: CandidateEvaluation, strategy_evidence: StrategyEvidence,
    policy: PromotionPolicy | None = None,
    selection: SelectionEvidence | None = None,
) -> str:
    """Digest the complete declared assessment, not authenticated market/model evidence."""
    return _hash(_assessment_payload(evaluation, strategy_evidence, policy, selection))


def _restore_dataclass(cls, payload, decimal_fields):
    if not isinstance(payload, dict) or set(payload) != {field.name for field in fields(cls)}:
        raise ValueError("candidate_approval_assessment_fields_invalid")
    values = dict(payload)
    for name in decimal_fields:
        raw = values[name]
        if not isinstance(raw, str) or not raw or raw != raw.strip() or len(raw) > 128:
            raise ValueError("candidate_approval_assessment_number_invalid")
        try:
            values[name] = Decimal(raw)
        except ArithmeticError as error:
            raise ValueError("candidate_approval_assessment_number_invalid") from error
    try:
        return cls(**values)
    except (ValueError, TypeError, AttributeError, ArithmeticError) as error:
        raise ValueError("candidate_approval_assessment_invalid") from error


def _check_approval(candidate_id, assessment, evidence_sha256):
    if not isinstance(assessment, dict):
        raise TypeError("candidate_approval_assessment_required")
    if assessment.get("schema") != ASSESSMENT_SCHEMA:
        raise ValueError("candidate_approval_assessment_schema_invalid")
    evaluation = _restore_dataclass(CandidateEvaluation, assessment.get("evaluation"),
        {"brier_score", "baseline_brier_score", "after_cost_expectancy", "max_drawdown"})
    evidence = _restore_dataclass(StrategyEvidence, assessment.get("strategy_evidence"),
        {"expectancy", "max_drawdown", "profit_factor"})
    policy = _restore_dataclass(PromotionPolicy, assessment.get("policy"),
        {"min_expectancy", "max_drawdown", "min_profit_factor", "min_deflated_sharpe"})
    raw_selection = assessment.get("selection")
    selection = (
        None if raw_selection is None
        else _restore_dataclass(SelectionEvidence, raw_selection, set())
    )
    recomputed = _assessment_payload(evaluation, evidence, policy, selection)
    if evaluation.candidate_id != candidate_id:
        raise ValueError("candidate_approval_candidate_mismatch")
    # Compare canonical encodings as well as hashes: True must not equal integer 1.
    if _hash(assessment) != _hash(recomputed) or evidence_sha256 != _hash(recomputed):
        raise ValueError("candidate_approval_assessment_digest_mismatch")
    if not recomputed["paper_ready"] or recomputed["live_ready"] is not False:
        raise ValueError("candidate_approval_assessment_failed")


@contextmanager
def _registry_lock(path, *, writing):
    """Cooperating local readers/writers only; no cross-host or authenticated authority."""
    try:
        import fcntl
    except ImportError as error:
        raise RuntimeError("candidate_registry_locking_unsupported") from error
    target = Path(path)
    if target.is_symlink():
        raise ValueError("candidate_registry_symlink_unsupported")
    if writing:
        target.parent.mkdir(parents=True, exist_ok=True)
    flags = (os.O_RDWR | os.O_CREAT) if writing else os.O_RDONLY
    try:
        fd = os.open(target, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    except FileNotFoundError:
        if writing:
            raise
        yield None
        return
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("candidate_registry_regular_unaliased_file_required")
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if writing else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN}:
                raise ValueError("candidate_registry_busy") from error
            raise
        if os.fstat(fd).st_size > MAX_REGISTRY_BYTES:
            raise ValueError("candidate_registry_size_limit")
        yield target
    finally:
        os.close(fd)


def _unique_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("candidate_registry_duplicate_json_key")
        value[key] = item
    return value


def _nonfinite_json(_value):
    raise ValueError("candidate_registry_nonfinite_json")


def _read_registry_records(path):
    # The writer holds the same file lock through validation and append. Do not extend
    # a complete JSON object missing its newline: append_record would concatenate it.
    raw = Path(path).read_bytes()
    if len(raw) > MAX_REGISTRY_BYTES:
        raise ValueError("candidate_registry_size_limit")
    if raw and not raw.endswith(b"\n"):
        raise ValueError("candidate_registry_incomplete_record")
    records = []
    for line in raw.split(b"\n"):
        if not line.strip():
            continue
        record = json.loads(line.decode("utf-8"), object_pairs_hook=_unique_keys,
                            parse_constant=_nonfinite_json)
        if not isinstance(record, dict) or not record:
            raise ValueError("candidate_registry_record_invalid")
        records.append(record)
    return records


def _replay(records):
    if not verify_chain(records):
        raise ValueError("ai_candidate_registry_chain_broken")
    stages = {}
    previous_time = None
    legacy_fields = {"schema", "candidate_id", "from_stage", "stage", "evidence_sha256", "reviewer", "live_execution_authorized"}
    for record in records:
        try:
            moment = _instant(datetime.fromisoformat(record["recorded_at"]))
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("candidate_registry_time_invalid") from error
        if previous_time is not None and moment < previous_time:
            raise ValueError("candidate_registry_time_regressed")
        previous_time = moment
        if record.get("event_type") != EVENT:
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("schema") not in {LEGACY_SCHEMA, SCHEMA}:
            raise ValueError("invalid_ai_candidate_registry_record")
        legacy = payload["schema"] == LEGACY_SCHEMA
        if set(payload) != (legacy_fields if legacy else legacy_fields | {"assessment"}):
            raise ValueError("candidate_registry_fields_invalid")
        candidate_id = payload["candidate_id"]
        _text(candidate_id, "identity")
        _text(payload["reviewer"], "reviewer", optional=True)
        _digest(payload["evidence_sha256"])
        if payload["live_execution_authorized"] is not False:
            raise ValueError("candidate_registry_live_authority_forbidden")
        try:
            current = stages.get(candidate_id, CandidateStage.RESEARCH)
            declared_from = CandidateStage(payload["from_stage"])
            target = CandidateStage(payload["stage"])
        except (TypeError, ValueError) as error:
            raise ValueError("candidate_registry_transition_invalid") from error
        if current is not declared_from or target not in _ALLOWED[current]:
            raise ValueError("candidate_registry_transition_invalid")
        if target is CandidateStage.PAPER_APPROVED:
            if legacy:
                raise ValueError("candidate_legacy_approval_requires_review")
            if payload["reviewer"] is None:
                raise ValueError("paper_candidate_requires_named_reviewer")
            _check_approval(candidate_id, payload["assessment"], payload["evidence_sha256"])
        elif not legacy and payload["assessment"] is not None:
            raise ValueError("candidate_assessment_only_for_approval")
        stages[candidate_id] = target
    return stages, previous_time


def transition_candidate(
    path: str | Path,
    *,
    candidate_id: str,
    target: CandidateStage,
    evidence_sha256: str,
    reviewer: str | None = None,
    now: datetime | None = None,
    evaluation: CandidateEvaluation | None = None,
    strategy_evidence: StrategyEvidence | None = None,
    policy: PromotionPolicy | None = None,
    selection: SelectionEvidence | None = None,
) -> dict:
    _text(candidate_id, "identity")
    _digest(evidence_sha256)
    if not isinstance(target, CandidateStage):
        raise TypeError("candidate_target_must_be_stage_enum")
    if target is CandidateStage.PAPER_APPROVED and (reviewer is None or reviewer == ""):
        raise ValueError("paper_candidate_requires_named_reviewer")
    _text(reviewer, "reviewer", optional=True)
    moment = _instant(now if now is not None else datetime.now(timezone.utc))
    assessment = None
    if target is CandidateStage.PAPER_APPROVED:
        if evaluation is None or strategy_evidence is None:
            raise ValueError("candidate_approval_assessment_required")
        assessment = _assessment_payload(evaluation, strategy_evidence, policy, selection)
        _check_approval(candidate_id, assessment, evidence_sha256)
    elif any(value is not None for value in (evaluation, strategy_evidence, policy, selection)):
        raise ValueError("candidate_assessment_only_for_approval")
    with _registry_lock(path, writing=True) as target_path:
        stages, previous_time = _replay(_read_registry_records(target_path))
        if previous_time is not None and moment < previous_time:
            raise ValueError("candidate_registry_time_regressed")
        current = stages.get(candidate_id, CandidateStage.RESEARCH)
        if target not in _ALLOWED[current]:
            raise ValueError(f"invalid_candidate_transition:{current.value}->{target.value}")
        payload = {
            "schema": SCHEMA, "candidate_id": candidate_id, "from_stage": current.value,
            "stage": target.value, "evidence_sha256": evidence_sha256, "reviewer": reviewer,
            "live_execution_authorized": False, "assessment": assessment,
        }
        return append_record(target_path, EVENT, payload, now=moment)


def candidate_stages(path: str | Path) -> dict[str, CandidateStage]:
    with _registry_lock(path, writing=False) as target:
        if target is None:
            return {}
        stages, _ = _replay(_read_registry_records(target))
        return stages
