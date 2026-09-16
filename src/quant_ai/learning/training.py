"""Reproducible training-run wrapper.  It trains candidates; it never promotes them."""
from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime, timezone

from quant_ai.learning.contracts import TrainingDatasetManifest, TrainingRunManifest

Trainer = Callable[[TrainingDatasetManifest, int], bytes]


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
    moment = trained_at or datetime.now(timezone.utc)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("trained_at_must_be_timezone_aware")
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
    )
    return artifact, manifest
