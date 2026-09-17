"""Reproducible training-run wrapper.  It trains candidates; it never promotes them."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timezone

from quant_ai.learning.contracts import (
    TrainingDatasetManifest,
    TrainingRunManifest,
    validate_training_run_inputs,
)

Trainer = Callable[[TrainingDatasetManifest, int], bytes]


def training_dataset_digest(dataset: TrainingDatasetManifest) -> str:
    """Bind declared provenance, not dataset bytes or provider authenticity."""
    if not isinstance(dataset, TrainingDatasetManifest):
        raise TypeError("training_dataset_manifest_required")
    payload = {
        "schema": "pramana.training_dataset_manifest.v1", "dataset_id": dataset.dataset_id,
        "cutoff": dataset.cutoff.astimezone(timezone.utc).isoformat(),
        "row_count": dataset.row_count, "source_ids": list(dataset.source_ids),
        "source_snapshot_sha256": dataset.source_snapshot_sha256,
        "feature_schema_sha256": dataset.feature_schema_sha256,
        "label_schema_sha256": dataset.label_schema_sha256,
        "cost_policy_id": dataset.cost_policy_id,
        "adjustment_policy_id": dataset.adjustment_policy_id,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def execute_training(
    dataset: TrainingDatasetManifest,
    *,
    run_id: str,
    candidate_id: str,
    model_family: str,
    code_sha256: str,
    configuration_sha256: str,
    seed: int,
    trainer: Trainer,
    trained_at: datetime | None = None,
) -> tuple[bytes, TrainingRunManifest]:
    """Run a caller-supplied deterministic trainer and bind the resulting artifact.

    Raw datasets stay outside this module.  The manifest pins the point-in-time snapshot,
    feature/label contracts and cost/adjustment policy, while the artifact digest makes a
    candidate reproducible and reviewable.  Nothing here can change a traded model.
    """
    dataset_digest = training_dataset_digest(dataset)
    moment = trained_at if trained_at is not None else datetime.now(timezone.utc)
    validate_training_run_inputs(
        run_id=run_id, candidate_id=candidate_id, model_family=model_family,
        dataset_id=dataset.dataset_id, trained_at=moment, seed=seed,
        code_sha256=code_sha256, configuration_sha256=configuration_sha256,
    )
    if not callable(trainer):
        raise TypeError("training_trainer_must_be_callable")
    if moment < dataset.cutoff:
        raise ValueError("training_before_dataset_cutoff")
    artifact = trainer(dataset, seed)
    if not isinstance(artifact, bytes) or not artifact:
        raise ValueError("trainer_must_return_nonempty_artifact_bytes")
    manifest = TrainingRunManifest(
        run_id=run_id,
        candidate_id=candidate_id,
        model_family=model_family,
        dataset_id=dataset.dataset_id,
        trained_at=moment,
        seed=seed,
        code_sha256=code_sha256,
        configuration_sha256=configuration_sha256,
        artifact_sha256=hashlib.sha256(artifact).hexdigest(),
        dataset_manifest_sha256=dataset_digest,
    )
    return artifact, manifest
