"""Immutable contracts for AI knowledge access and reproducible training."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

_SHA256 = re.compile(r"[0-9a-f]{64}")


def _aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name}_must_be_datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name}_must_be_timezone_aware")


def _digest(value: str, name: str) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{name}_must_be_sha256")


def _text(value: str, name: str) -> None:
    # Metadata is rendered into prompts and manifests, so physical/control-line
    # boundaries must not be supplied by a source or confused with a policy field.
    if (not isinstance(value, str) or not value or value != value.strip()
            or any(not char.isprintable() for char in value)):
        raise ValueError(f"{name}_must_be_nonempty_single_line_text")


def _enum_scope(values, kind, name):
    if not isinstance(values, (tuple, list, set, frozenset)) or not values:
        raise ValueError(f"{name}_required")
    if any(not isinstance(value, kind) for value in values):
        raise TypeError(f"{name}_must_use_declared_enum")
    return frozenset(values)


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
        _text(self.source_id, "knowledge_source_id")
        _text(self.provider, "knowledge_source_provider")
        object.__setattr__(self, "categories", _enum_scope(
            self.categories, KnowledgeCategory, "knowledge_source_categories"))
        object.__setattr__(self, "planes", _enum_scope(
            self.planes, AccessPlane, "knowledge_source_planes"))
        if type(self.point_in_time) is not bool:
            raise TypeError("knowledge_source_point_in_time_must_be_boolean")
        if not isinstance(self.rights_status, RightsStatus):
            raise TypeError("knowledge_source_rights_must_use_declared_enum")
        if self.max_age_seconds is not None and (
            type(self.max_age_seconds) is not int or self.max_age_seconds <= 0
        ):
            raise ValueError("knowledge_source_max_age_must_be_positive_integer")


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
        _text(self.item_id, "knowledge_item_id")
        _text(self.source_id, "knowledge_item_source")
        _text(self.reference, "knowledge_item_reference")
        if not isinstance(self.category, KnowledgeCategory):
            raise TypeError("knowledge_item_category_must_use_declared_enum")
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
        _text(self.dataset_id, "training_dataset_id")
        if type(self.row_count) is not int or self.row_count < 1:
            raise ValueError("training_dataset_rows_must_be_positive_integer")
        _aware(self.cutoff, "training_cutoff")
        if not isinstance(self.source_ids, (tuple, list)) or not self.source_ids:
            raise ValueError("training_dataset_ordered_sources_required")
        for source in self.source_ids:
            _text(source, "training_dataset_source")
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("training_dataset_unique_sources_required")
        object.__setattr__(self, "source_ids", tuple(self.source_ids))
        for value, name in (
            (self.source_snapshot_sha256, "source_snapshot"),
            (self.feature_schema_sha256, "feature_schema"),
            (self.label_schema_sha256, "label_schema"),
        ):
            _digest(value, name)
        _text(self.cost_policy_id, "training_dataset_cost_policy")
        _text(self.adjustment_policy_id, "training_dataset_adjustment_policy")


def validate_training_run_inputs(*, run_id, candidate_id, model_family, dataset_id,
                                 trained_at, seed, code_sha256, configuration_sha256) -> None:
    """Preflight before any caller-supplied trainer can execute or incur work."""
    for value in (run_id, candidate_id, model_family, dataset_id):
        _text(value, "training_run_identity")
    _aware(trained_at, "trained_at")
    if type(seed) is not int:
        raise TypeError("training_seed_must_be_integer")
    _digest(code_sha256, "code")
    _digest(configuration_sha256, "configuration")


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
    # Older manifests did not bind the full dataset contract. None remains unknown;
    # it must never be backfilled by guessing a snapshot from a reused dataset name.
    dataset_manifest_sha256: str | None = None

    def __post_init__(self) -> None:
        validate_training_run_inputs(
            run_id=self.run_id, candidate_id=self.candidate_id, model_family=self.model_family,
            dataset_id=self.dataset_id, trained_at=self.trained_at, seed=self.seed,
            code_sha256=self.code_sha256, configuration_sha256=self.configuration_sha256,
        )
        _digest(self.artifact_sha256, "artifact")
        if self.dataset_manifest_sha256 is not None:
            _digest(self.dataset_manifest_sha256, "dataset_manifest")


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
