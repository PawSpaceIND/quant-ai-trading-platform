"""Manifest binding and preflight; no real data or model is trained here."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256

import pytest

from quant_ai.learning.contracts import TrainingDatasetManifest
from quant_ai.learning.training import execute_training

NOW = datetime(2026, 9, 17, 6, tzinfo=timezone.utc)
H = "a" * 64


def dataset(**changes):
    values = {"dataset_id": "synthetic-dataset", "cutoff": NOW, "row_count": 20,
              "source_ids": ("synthetic-source",), "source_snapshot_sha256": H,
              "feature_schema_sha256": H, "label_schema_sha256": H,
              "cost_policy_id": "synthetic-cost", "adjustment_policy_id": "synthetic-adjustment"}
    values.update(changes)
    return TrainingDatasetManifest(**values)


def run(data=None, **changes):
    values = {"run_id": "run", "candidate_id": "candidate", "model_family": "synthetic",
              "code_sha256": H, "configuration_sha256": H, "seed": 7,
              "trainer": lambda _data, _seed: b"synthetic-artifact", "trained_at": NOW}
    values.update(changes)
    return execute_training(data or dataset(), **values)


@pytest.mark.parametrize("field,value", [("run_id", ""), ("candidate_id", ""),
    ("model_family", ""), ("code_sha256", "bad"), ("configuration_sha256", "bad"),
    ("seed", True), ("seed", 1.5)])
def test_invalid_training_request_never_invokes_trainer(field, value):
    called = []
    def trainer(_data, _seed):
        called.append(True)
        return b"synthetic-artifact"
    with pytest.raises((ValueError, TypeError)):
        run(**{field: value, "trainer": trainer})
    assert called == []


@pytest.mark.parametrize("field,value", [("row_count", True), ("row_count", 1.5),
    ("row_count", float("nan")), ("source_ids", "synthetic-source")])
def test_dataset_shape_is_validated_before_training(field, value):
    with pytest.raises((ValueError, TypeError), match="training_dataset"):
        dataset(**{field: value})


def test_caller_cannot_mutate_dataset_sources_after_manifest_construction():
    sources = ["synthetic-source"]
    data = dataset(source_ids=sources)
    sources.append("future-source")
    assert data.source_ids == ("synthetic-source",)


@pytest.mark.parametrize("field,value", [("row_count", 21), ("source_ids", ("other-source",)),
    ("source_snapshot_sha256", "b"*64), ("feature_schema_sha256", "b"*64),
    ("label_schema_sha256", "b"*64), ("cost_policy_id", "other-cost"),
    ("adjustment_policy_id", "other-adjustment"), ("cutoff", NOW-timedelta(seconds=1))])
def test_same_dataset_name_cannot_hide_changed_training_contract(field, value):
    original = dataset()
    _, first = run(original)
    _, changed = run(replace(original, **{field: value}))
    assert first.dataset_id == changed.dataset_id
    assert first.dataset_manifest_sha256 != changed.dataset_manifest_sha256


def test_bound_manifest_hash_and_artifact_are_repeatable():
    from quant_ai.learning.training import training_dataset_digest
    data = dataset()
    artifact, first = run(data)
    _, second = run(data)
    assert first.dataset_manifest_sha256 == training_dataset_digest(data)
    assert first == second
    assert first.artifact_sha256 == sha256(artifact).hexdigest()


def test_training_digest_uses_same_instant_across_timezones():
    from quant_ai.learning.training import training_dataset_digest
    first = dataset()
    other = replace(first, cutoff=NOW.astimezone(timezone(timedelta(hours=5, minutes=30))))
    assert training_dataset_digest(first) == training_dataset_digest(other)


def test_legacy_run_has_unknown_dataset_binding_not_fabricated_evidence():
    from dataclasses import asdict

    from quant_ai.learning.contracts import TrainingRunManifest
    _, generated = run()
    values = asdict(generated)
    values.pop("dataset_manifest_sha256")
    legacy = TrainingRunManifest(**values)
    assert legacy.dataset_manifest_sha256 is None
    assert legacy.artifact_sha256 == generated.artifact_sha256


def test_new_dataset_digest_requires_valid_sha256_when_supplied():
    _, manifest = run()
    with pytest.raises(ValueError, match="dataset_manifest_must_be_sha256"):
        replace(manifest, dataset_manifest_sha256="not-evidence")


def test_trainer_failure_does_not_produce_a_run_manifest():
    def trainer(_data, _seed):
        raise RuntimeError("synthetic_training_failure")
    with pytest.raises(RuntimeError, match="synthetic_training_failure"):
        run(trainer=trainer)
