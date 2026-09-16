from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant_ai.learning.candidates import (
    CandidateStage,
    assess_candidate,
    candidate_stages,
    transition_candidate,
)
from quant_ai.learning.contracts import (
    AccessPlane,
    CandidateEvaluation,
    KnowledgeCategory,
    KnowledgeItem,
    RightsStatus,
    SourceGrant,
    TrainingDatasetManifest,
)
from quant_ai.learning.knowledge import KnowledgeAccessController
from quant_ai.learning.training import execute_training
from quant_ai.validation.promotion import StrategyEvidence

NOW = datetime(2026, 9, 16, 10, tzinfo=timezone.utc)
H = "a" * 64


def item(source="market", category=KnowledgeCategory.MARKET, *, observed=NOW, available=NOW):
    return KnowledgeItem(f"item-{source}-{observed.timestamp()}-{available.timestamp()}", source, category, observed, available, H, "evidence://item")


def test_decision_access_is_point_in_time_rights_checked_and_staleness_checked():
    grants = (
        SourceGrant("market", "broker", frozenset({KnowledgeCategory.MARKET}),
                    frozenset({AccessPlane.RESEARCH, AccessPlane.TRAINING, AccessPlane.DECISION}),
                    True, RightsStatus.VERIFIED, 60),
        SourceGrant("rumor", "unverified-web", frozenset({KnowledgeCategory.NEWS}),
                    frozenset({AccessPlane.RESEARCH, AccessPlane.DECISION}),
                    True, RightsStatus.UNVERIFIED, 300),
    )
    access = KnowledgeAccessController(
        grants, required_decision_categories=(KnowledgeCategory.MARKET,)
    )
    future = item(observed=NOW, available=NOW + timedelta(seconds=1))
    stale = item(source="market", observed=NOW - timedelta(seconds=61), available=NOW - timedelta(seconds=61))
    rumor = item(source="rumor", category=KnowledgeCategory.NEWS)
    selection = access.select((future, stale, rumor), plane=AccessPlane.DECISION, as_of=NOW)
    assert not selection.usable
    assert [row.reason for row in selection.rejections] == [
        "future_evidence", "stale_evidence", "source_rights_unverified"
    ]
    assert selection.missing_required == (KnowledgeCategory.MARKET,)
    # The same explicitly unverified material can still be inspected in research, where
    # it remains labelled rather than leaking into a decision or training set.
    research = access.select((rumor,), plane=AccessPlane.RESEARCH, as_of=NOW)
    assert research.items == (rumor,)


def test_training_manifest_and_artifact_are_reproducible_and_cutoff_bound():
    dataset = TrainingDatasetManifest(
        "ds-1", NOW, 1000, ("market", "macro"), H, H, H, "cost-v1", "adjusted-v1"
    )
    trainer = lambda manifest, seed: f"{manifest.dataset_id}:{seed}".encode()
    artifact1, run1 = execute_training(
        dataset, run_id="run-1", candidate_id="candidate-1", model_family="gradient-boosted",
        code_sha256=H, configuration_sha256=H, seed=7, trainer=trainer,
        trained_at=NOW + timedelta(minutes=1),
    )
    artifact2, run2 = execute_training(
        dataset, run_id="run-2", candidate_id="candidate-1", model_family="gradient-boosted",
        code_sha256=H, configuration_sha256=H, seed=7, trainer=trainer,
        trained_at=NOW + timedelta(minutes=1),
    )
    assert artifact1 == artifact2
    assert run1.artifact_sha256 == run2.artifact_sha256
    with pytest.raises(ValueError, match="training_before_dataset_cutoff"):
        execute_training(
            dataset, run_id="bad", candidate_id="c", model_family="m", code_sha256=H,
            configuration_sha256=H, seed=1, trainer=trainer,
            trained_at=NOW - timedelta(seconds=1),
        )


def evaluation(*, brier="0.18", baseline="0.25", expectancy="10", resolved=120):
    return CandidateEvaluation(
        "candidate-1", resolved, Decimal(brier), Decimal(baseline), Decimal(expectancy),
        Decimal("0.06"), H, H, H, H, H,
    )


def strategy():
    return StrategyEvidence(120, Decimal(10), Decimal("0.06"), Decimal("1.4"), 3, 35)


def test_candidate_requires_after_cost_edge_and_probability_skill_but_never_live_approves():
    good = assess_candidate(evaluation(), strategy())
    assert good.shadow_ready and good.paper_ready
    assert not good.live_ready
    bad = assess_candidate(evaluation(brier="0.30", baseline="0.25"), strategy())
    assert not bad.paper_ready
    assert "candidate_probability_skill_not_better_than_baseline" in bad.reasons


def test_candidate_registry_is_hash_chained_and_requires_review_for_paper_approval(tmp_path):
    path = tmp_path / "candidates.jsonl"
    transition_candidate(path, candidate_id="candidate-1", target=CandidateStage.SHADOW,
                         evidence_sha256=H, now=NOW)
    transition_candidate(path, candidate_id="candidate-1", target=CandidateStage.PAPER_CANDIDATE,
                         evidence_sha256=H, now=NOW + timedelta(seconds=1))
    with pytest.raises(ValueError, match="requires_named_reviewer"):
        transition_candidate(path, candidate_id="candidate-1", target=CandidateStage.PAPER_APPROVED,
                             evidence_sha256=H, now=NOW + timedelta(seconds=2))
    transition_candidate(path, candidate_id="candidate-1", target=CandidateStage.PAPER_APPROVED,
                         evidence_sha256=H, reviewer="founder-review",
                         now=NOW + timedelta(seconds=2))
    assert candidate_stages(path)["candidate-1"] is CandidateStage.PAPER_APPROVED
