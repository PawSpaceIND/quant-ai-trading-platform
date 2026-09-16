"""Champion/challenger governance for AI candidates in shadow and paper modes only."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

from quant_ai.learning.contracts import CandidateEvaluation
from quant_ai.operations.evidence_log import append_record, read_records, verify_chain
from quant_ai.validation.promotion import (
    PromotionDecision,
    PromotionPolicy,
    StrategyEvidence,
    evaluate_promotion,
)

_SHA256 = re.compile(r"[0-9a-f]{64}")
EVENT = "ai_candidate_stage"
SCHEMA = "pramana.ai_candidate_registry.v1"


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
) -> CandidateAssessment:
    """Demand both trading evidence and probability skill; never approve live money."""
    chosen = policy or PromotionPolicy()
    strategy = evaluate_promotion(strategy_evidence, chosen)
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


def _current_stages(path: str | Path) -> dict[str, CandidateStage]:
    records = read_records(path)
    if not verify_chain(records):
        raise ValueError("ai_candidate_registry_chain_broken")
    stages: dict[str, CandidateStage] = {}
    for record in records:
        if record.get("event_type") != EVENT:
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
            raise ValueError("invalid_ai_candidate_registry_record")
        stages[str(payload["candidate_id"])] = CandidateStage(str(payload["stage"]))
    return stages


def transition_candidate(
    path: str | Path,
    *,
    candidate_id: str,
    target: CandidateStage,
    evidence_sha256: str,
    reviewer: str | None = None,
    now: datetime | None = None,
) -> dict:
    if not candidate_id.strip() or not _SHA256.fullmatch(evidence_sha256):
        raise ValueError("candidate_transition_identity_and_evidence_required")
    stages = _current_stages(path)
    current = stages.get(candidate_id, CandidateStage.RESEARCH)
    if target not in _ALLOWED[current]:
        raise ValueError(f"invalid_candidate_transition:{current.value}->{target.value}")
    if target is CandidateStage.PAPER_APPROVED and not (reviewer or "").strip():
        raise ValueError("paper_candidate_requires_named_reviewer")
    payload = {
        "schema": SCHEMA,
        "candidate_id": candidate_id,
        "from_stage": current.value,
        "stage": target.value,
        "evidence_sha256": evidence_sha256,
        "reviewer": (reviewer or "").strip() or None,
        "live_execution_authorized": False,
    }
    return append_record(path, EVENT, payload, now=now)


def candidate_stages(path: str | Path) -> dict[str, CandidateStage]:
    return _current_stages(path)
