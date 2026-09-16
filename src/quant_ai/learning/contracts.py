"""Immutable contracts for AI knowledge access and reproducible training."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

_SHA256 = re.compile(r"[0-9a-f]{64}")


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name}_must_be_timezone_aware")


def _digest(value: str, name: str) -> None:
    if not _SHA256.fullmatch(value):
        raise ValueError(f"{name}_must_be_sha256")


class KnowledgeCategory(str, Enum):
    MARKET = "MARKET"
    ORDER_BOOK = "ORDER_BOOK"
    FUNDAMENTAL = "FUNDAMENTAL"
    MACRO = "MACRO"
    NEWS = "NEWS"
    CORPORATE_EVENT = "CORPORATE_EVENT"
    DERIVATIVES = "DERIVATIVES"
    BROKER = "BROKER"
    PORTFOLIO = "PORTFOLIO"
    RISK = "RISK"
    EXECUTION = "EXECUTION"
    DECISION_HISTORY = "DECISION_HISTORY"
    RESEARCH = "RESEARCH"
    OPERATOR_LESSON = "OPERATOR_LESSON"


class AccessPlane(str, Enum):
    RESEARCH = "RESEARCH"
    TRAINING = "TRAINING"
    DECISION = "DECISION"


class RightsStatus(str, Enum):
    INTERNAL = "INTERNAL"
    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"


@dataclass(frozen=True)
class SourceGrant:
    source_id: str
    provider: str
    categories: frozenset[KnowledgeCategory]
    planes: frozenset[AccessPlane]
    point_in_time: bool
    rights_status: RightsStatus
    max_age_seconds: int | None = None

    def __post_init__(self) -> None:
        if not self.source_id.strip() or not self.provider.strip():
            raise ValueError("knowledge_source_identity_required")
        if not self.categories or not self.planes:
            raise ValueError("knowledge_source_scope_required")
        if self.max_age_seconds is not None and self.max_age_seconds <= 0:
            raise ValueError("knowledge_source_max_age_must_be_positive")


@dataclass(frozen=True)
class KnowledgeItem:
    item_id: str
    source_id: str
    category: KnowledgeCategory
    observed_at: datetime
    available_at: datetime
    content_sha256: str
    reference: str

    def __post_init__(self) -> None:
        if not self.item_id.strip() or not self.source_id.strip() or not self.reference.strip():
            raise ValueError("knowledge_item_identity_required")
        _aware(self.observed_at, "observed_at")
        _aware(self.available_at, "available_at")
        if self.available_at < self.observed_at:
            raise ValueError("knowledge_available_before_observed")
        _digest(self.content_sha256, "content")


@dataclass(frozen=True)
class TrainingDatasetManifest:
    dataset_id: str
    cutoff: datetime
    row_count: int
    source_ids: tuple[str, ...]
    source_snapshot_sha256: str
    feature_schema_sha256: str
    label_schema_sha256: str
    cost_policy_id: str
    adjustment_policy_id: str

    def __post_init__(self) -> None:
        if not self.dataset_id.strip() or self.row_count < 1:
            raise ValueError("training_dataset_identity_and_rows_required")
        _aware(self.cutoff, "training_cutoff")
        if not self.source_ids or len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("training_dataset_unique_sources_required")
        if any(not item.strip() for item in self.source_ids):
            raise ValueError("training_dataset_source_identity_required")
        for value, name in (
            (self.source_snapshot_sha256, "source_snapshot"),
            (self.feature_schema_sha256, "feature_schema"),
            (self.label_schema_sha256, "label_schema"),
        ):
            _digest(value, name)
        if not self.cost_policy_id.strip() or not self.adjustment_policy_id.strip():
            raise ValueError("training_dataset_cost_and_adjustment_policy_required")


@dataclass(frozen=True)
class TrainingRunManifest:
    run_id: str
    candidate_id: str
    model_family: str
    dataset_id: str
    trained_at: datetime
    seed: int
    code_sha256: str
    configuration_sha256: str
    artifact_sha256: str

    def __post_init__(self) -> None:
        if any(not value.strip() for value in (
            self.run_id, self.candidate_id, self.model_family, self.dataset_id
        )):
            raise ValueError("training_run_identity_required")
        _aware(self.trained_at, "trained_at")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise TypeError("training_seed_must_be_integer")
        for value, name in (
            (self.code_sha256, "code"),
            (self.configuration_sha256, "configuration"),
            (self.artifact_sha256, "artifact"),
        ):
            _digest(value, name)


@dataclass(frozen=True)
class CandidateEvaluation:
    candidate_id: str
    resolved_probability_count: int
    brier_score: Decimal
    baseline_brier_score: Decimal
    after_cost_expectancy: Decimal
    max_drawdown: Decimal
    holdout_sha256: str
    forward_paper_sha256: str
    calibration_sha256: str
    execution_stress_sha256: str
    trial_register_sha256: str

    def __post_init__(self) -> None:
        if not self.candidate_id.strip() or self.resolved_probability_count < 0:
            raise ValueError("candidate_evaluation_identity_required")
        if not Decimal(0) <= self.brier_score <= Decimal(1):
            raise ValueError("candidate_brier_out_of_range")
        if not Decimal(0) <= self.baseline_brier_score <= Decimal(1):
            raise ValueError("baseline_brier_out_of_range")
        if not Decimal(0) <= self.max_drawdown <= Decimal(1):
            raise ValueError("candidate_drawdown_out_of_range")
        for value, name in (
            (self.holdout_sha256, "holdout"),
            (self.forward_paper_sha256, "forward_paper"),
            (self.calibration_sha256, "calibration"),
            (self.execution_stress_sha256, "execution_stress"),
            (self.trial_register_sha256, "trial_register"),
        ):
            _digest(value, name)
