"""Synthetic AI recovery; never load artifacts, train models or activate approvals."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import timedelta

import pytest
from test_ai_learning_governance import NOW, H
from test_candidate_registry_integrity import approval_args, staged
from test_recovery_bundle import fixture as account_fixture

from quant_ai.learning import candidates
from quant_ai.learning.contracts import TrainingDatasetManifest, TrainingRunManifest
from quant_ai.learning.training import training_dataset_digest
from quant_ai.operations import recovery_bundle as bundle


def fixture(tmp_path):
    spec = account_fixture(tmp_path)
    root = tmp_path / "ai"
    root.mkdir()
    log = root / "registry.jsonl"
    staged(log)
    candidates.transition_candidate(log, **approval_args())
    dataset = TrainingDatasetManifest("dataset-1", NOW - timedelta(days=1), 10,
        ("synthetic-source",), H, H, H, "synthetic-cost", "synthetic-adjustment")
    artifact = b"synthetic opaque model bytes; not executable"
    run = TrainingRunManifest("run-1", "candidate-1", "synthetic-model", dataset.dataset_id,
        NOW - timedelta(hours=1), 7, H, H, hashlib.sha256(artifact).hexdigest(),
        training_dataset_digest(dataset))
    for name, value in (("dataset.json", dataset), ("run.json", run)):
        (root / name).write_text(json.dumps(asdict(value), default=lambda v: v.isoformat()))
    (root / "artifact.bin").write_bytes(artifact)
    spec["ai_state"] = {"tenant": spec["tenant"], "registry": str(log), "candidates": {
        "candidate-1": {"run_manifest": str(root / "run.json"),
            "dataset_manifest": str(root / "dataset.json"), "artifact": str(root / "artifact.bin")}}}
    return spec, root


def test_ai_bundle_preserves_registry_training_and_artifact_without_activation(tmp_path):
    spec, root = fixture(tmp_path)
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    manifest = bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    assert manifest["schema"] == 5
    result = bundle.restore(tmp_path / "backup", tmp_path / "restored", manifest_sha256=manifest["manifestSha256"])
    assert result["status"] == "restored"
    ai = result["aiRecovery"]
    assert ai["status"] == "selected_state_verified"
    assert ai["recordedStages"] == {"candidate-1": "PAPER_APPROVED"}
    assert ai["activationAuthorized"] is False and ai["liveExecutionAuthorized"] is False
    assert ai["artifactApprovalBindingVerified"] is False
    assert ai["datasetBytesVerified"] is False
    assert {p.name: p.read_bytes() for p in root.iterdir()} == before
    assert (tmp_path / "restored/ai-state/registry.jsonl").read_bytes() == before["registry.jsonl"]
    assert (tmp_path / "restored/halt").exists()


def test_selected_artifact_must_match_recorded_training_hash(tmp_path):
    spec, root = fixture(tmp_path)
    (root / "artifact.bin").write_bytes(b"different artifact")
    with pytest.raises(ValueError, match="AI recovery artifact checksum"):
        bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    assert not (tmp_path / "backup").exists()


def test_missing_candidate_training_record_cannot_look_like_complete_capture(tmp_path):
    spec, _ = fixture(tmp_path)
    spec["ai_state"]["candidates"] = {}
    with pytest.raises(ValueError, match="AI recovery candidate inventory"):
        bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    assert not (tmp_path / "backup").exists()


def edit_json(path, change):
    value = json.loads(path.read_text())
    change(value)
    path.write_text(json.dumps(value))


def rehash_log(path, change):
    from quant_ai.operations.evidence_log import _digest
    records = [json.loads(line) for line in path.read_text().splitlines()]
    change(records)
    previous = "GENESIS"
    for index, record in enumerate(records, 1):
        record["previous_sha256"] = previous
        if type(record["sequence"]) is int:
            record["sequence"] = index
        record["sha256"] = _digest({k: v for k, v in record.items() if k != "sha256"})
        previous = record["sha256"]
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


@pytest.mark.parametrize("field,value", [
    ("tenant", "other"), ("tenant", 1), ("registry", ""), ("registry", ":memory:"),
    ("registry", None), ("candidates", []), ("candidates", None), ("unknown", True),
])
def test_invalid_ai_selection_never_creates_bundle(tmp_path, field, value):
    spec, _ = fixture(tmp_path)
    spec["ai_state"][field] = value
    with pytest.raises(ValueError, match="AI recovery"):
        bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    assert not (tmp_path / "backup").exists()


@pytest.mark.parametrize("filename", ["registry.jsonl", "run.json", "dataset.json", "artifact.bin"])
def test_missing_selected_file_is_not_an_empty_model_inventory(tmp_path, filename):
    spec, root = fixture(tmp_path)
    (root / filename).unlink()
    with pytest.raises(ValueError, match="AI recovery"):
        bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    assert not (root / filename).exists() and not (tmp_path / "backup").exists()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory"])
def test_aliased_or_nonregular_artifact_is_rejected_without_touching_target(tmp_path, kind):
    import os
    spec, root = fixture(tmp_path)
    path = root / "artifact.bin"
    target = tmp_path / "original.bin"
    path.rename(target)
    before = target.read_bytes()
    if kind == "symlink":
        path.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, path)
    else:
        path.mkdir()
    with pytest.raises(ValueError, match="AI recovery"):
        bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    assert target.read_bytes() == before


@pytest.mark.parametrize("field,value", [("candidate_id", "other"), ("dataset_id", "other"),
    ("dataset_manifest_sha256", None), ("dataset_manifest_sha256", "b" * 64),
    ("artifact_sha256", "f" * 64), ("seed", True), ("seed", 1.2),
    ("trained_at", "2026-09-16T09:00:00"), ("unknown", "extra")])
def test_invalid_training_contract_never_gets_verified_capture(tmp_path, field, value):
    spec, root = fixture(tmp_path)
    edit_json(root / "run.json", lambda r: r.update({field: value}))
    with pytest.raises(ValueError, match="AI recovery"):
        bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    assert not (tmp_path / "backup").exists()


@pytest.mark.parametrize("field,value", [("row_count", 11), ("source_ids", ["changed"]),
    ("feature_schema_sha256", "b" * 64), ("cost_policy_id", "changed"),
    ("cutoff", "2026-09-16T09:30:00+00:00"), ("row_count", True)])
def test_dataset_contract_change_cannot_reuse_training_binding(tmp_path, field, value):
    spec, root = fixture(tmp_path)
    edit_json(root / "dataset.json", lambda r: r.update({field: value}))
    with pytest.raises(ValueError, match="AI recovery"):
        bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    assert not (tmp_path / "backup").exists()


@pytest.mark.parametrize("defect", ["hash", "transition", "live", "assessment", "reviewer",
                                  "event", "future", "boolean_sequence", "no_newline"])
def test_registry_semantics_are_replayed_not_just_copied(tmp_path, defect):
    spec, root = fixture(tmp_path)
    path = root / "registry.jsonl"
    def change(records):
        if defect == "transition":
            records[-1]["payload"]["from_stage"] = "RESEARCH"
        elif defect == "live":
            records[-1]["payload"]["live_execution_authorized"] = True
        elif defect == "assessment":
            records[-1]["payload"]["assessment"]["evaluation"]["brier_score"] = "0.9"
        elif defect == "reviewer":
            records[-1]["payload"]["reviewer"] = None
        elif defect == "event":
            records[-1]["event_type"] = "unknown_event"
        elif defect == "future":
            records[-1]["recorded_at"] = "2999-01-01T00:00:00+00:00"
        elif defect == "boolean_sequence":
            records[0]["sequence"] = True
    if defect == "hash":
        path.write_text(path.read_text().replace('"SHADOW"', '"RESEARCH"', 1))
    elif defect == "no_newline":
        path.write_bytes(path.read_bytes().rstrip(b"\n"))
    else:
        rehash_log(path, change)
    before = path.read_bytes()
    with pytest.raises((ValueError, TypeError)):
        bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    assert path.read_bytes() == before and not (tmp_path / "backup").exists()


@pytest.mark.parametrize("raw", ['{"seed":1,"seed":2}', '{"seed":NaN}', '[]', '{', '\ufffd'])
def test_ambiguous_or_invalid_manifest_json_refuses(tmp_path, raw):
    spec, root = fixture(tmp_path)
    (root / "run.json").write_text(raw)
    with pytest.raises(ValueError, match="AI recovery manifest"):
        bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    assert not (tmp_path / "backup").exists()


def test_empty_registry_remains_empty_without_creating_approval(tmp_path):
    spec, root = fixture(tmp_path)
    (root / "registry.jsonl").write_bytes(b"")
    spec["ai_state"]["candidates"] = {}
    manifest = bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    report = bundle.restore(tmp_path / "backup", tmp_path / "restored", manifest_sha256=manifest["manifestSha256"])
    assert report["aiRecovery"]["registryRecords"] == 0
    assert report["aiRecovery"]["recordedStages"] == {}
    assert report["aiRecovery"]["activationAuthorized"] is False


def test_unselected_ai_state_is_explicitly_not_covered(tmp_path):
    spec = account_fixture(tmp_path)
    manifest = bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    assert manifest["schema"] == 2 and "aiState" not in manifest
    report = bundle.restore(tmp_path / "backup", tmp_path / "restored", manifest_sha256=manifest["manifestSha256"])
    assert report["aiRecovery"]["status"] == "not_selected"


def test_partial_or_duplicate_source_selection_refuses(tmp_path):
    spec, _ = fixture(tmp_path)
    entry = spec["ai_state"]["candidates"]["candidate-1"]
    entry["dataset_manifest"] = entry["run_manifest"]
    with pytest.raises(ValueError, match="must not overlap"):
        bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    del entry["artifact"]
    with pytest.raises(ValueError, match="candidate files required"):
        bundle.create(spec, tmp_path / "backup", writers_stopped=True)


@pytest.mark.parametrize("bound", ["oms", "institutional"])
def test_ai_schema_composes_with_existing_multi_store_bundles(tmp_path, bound):
    from test_institutional_state_bundle import fixture as institution
    from test_institutional_state_bundle import select
    from test_oms_recovery_bundle import selected
    left, right = tmp_path / "account", tmp_path / "learning"
    left.mkdir(); right.mkdir()
    if bound == "oms":
        spec, _, _ = selected(left)
    else:
        spec, _ = institution(left)
        select(spec)
    ai, _ = fixture(right)
    spec["ai_state"] = {**ai["ai_state"], "tenant": spec["tenant"]}
    manifest = bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    assert manifest["schema"] == 5 and "orderState" in manifest
    result = bundle.restore(tmp_path / "backup", tmp_path / "restored", manifest_sha256=manifest["manifestSha256"])
    assert result["status"] == "restored"
    assert result["aiRecovery"]["activationAuthorized"] is False
    assert result["orderRecovery"]["pathRebindRequired"] is True
    if bound == "institutional":
        assert result["institutionalRecovery"]["activationAuthorized"] is False


@pytest.mark.parametrize("defect", ["schema", "missing_ai", "tenant", "path", "missing_candidate", "result"])
def test_changed_ai_manifest_cannot_grant_a_restore(tmp_path, defect):
    spec, _ = fixture(tmp_path)
    bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    path = tmp_path / "backup/manifest.json"
    def change(m):
        if defect == "schema":
            m["schema"] = 2
        elif defect == "missing_ai":
            del m["aiState"]
        elif defect == "tenant":
            m["aiState"]["selection"]["tenant"] = "other"
        elif defect == "path":
            m["aiState"]["selection"]["registryPath"] = "../outside"
        elif defect == "missing_candidate":
            m["aiState"]["selection"]["candidates"] = {}
        else:
            m["aiState"]["verification"]["activationAuthorized"] = True
    edit_json(path, change)
    with pytest.raises(ValueError, match="AI recovery"):
        bundle.restore(tmp_path / "backup", tmp_path / "restored", manifest_sha256=bundle.digest(path))
    assert not (tmp_path / "restored").exists()


def test_changed_captured_artifact_fails_even_when_manifest_file_hash_is_updated(tmp_path):
    spec, _ = fixture(tmp_path)
    manifest = bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    name = manifest["aiState"]["selection"]["candidates"]["candidate-1"]["artifact"]
    artifact = tmp_path / "backup" / name
    artifact.write_bytes(b"wrong model bytes")
    meta = tmp_path / "backup/manifest.json"
    edit_json(meta, lambda m: m["files"][name].update(sha256=bundle.digest(artifact), size=artifact.stat().st_size))
    with pytest.raises(ValueError, match="artifact checksum"):
        bundle.restore(tmp_path / "backup", tmp_path / "restored", manifest_sha256=bundle.digest(meta))
    assert not (tmp_path / "restored").exists()


def test_training_and_model_loaders_are_never_called(tmp_path, monkeypatch):
    import pickle

    from quant_ai.learning import training
    from quant_ai.models.registry import ModelRegistry
    spec, _ = fixture(tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail("No training, loading, registry write or model activation allowed")
    monkeypatch.setattr(training, "execute_training", forbidden)
    monkeypatch.setattr(candidates, "transition_candidate", forbidden)
    monkeypatch.setattr(ModelRegistry, "register", forbidden)
    monkeypatch.setattr(pickle, "loads", forbidden)
    manifest = bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    result = bundle.restore(tmp_path / "backup", tmp_path / "restored", manifest_sha256=manifest["manifestSha256"])
    assert result["aiRecovery"]["activationAuthorized"] is False


def test_source_change_during_capture_never_overwrites_originals(tmp_path, monkeypatch):
    spec, root = fixture(tmp_path)
    original = bundle.sqlite_backup
    def interrupted(source, target):
        original(source, target)
        (root / "artifact.bin").write_bytes(b"source advanced")
    monkeypatch.setattr(bundle, "sqlite_backup", interrupted)
    with pytest.raises(ValueError, match="Source changed"):
        bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    assert (root / "artifact.bin").read_bytes() == b"source advanced"
    assert not (tmp_path / "backup").exists()


@pytest.mark.parametrize("limit", ["MAX_TOTAL_BYTES", "MAX_ARTIFACT_BYTES", "MAX_MANIFEST_BYTES", "MAX_CANDIDATES"])
def test_bounded_selection_rejects_oversize_before_copy(tmp_path, monkeypatch, limit):
    from quant_ai.operations import ai_recovery
    spec, _ = fixture(tmp_path)
    monkeypatch.setattr(ai_recovery, limit, 0)
    with pytest.raises(ValueError, match="AI recovery"):
        bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    assert not (tmp_path / "backup").exists()


@pytest.mark.parametrize("field,value", [("activationAuthorized", 0), ("registryRecords", 3.0)])
def test_restore_cannot_coerce_verification_types(tmp_path, field, value):
    spec, _ = fixture(tmp_path)
    bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    meta = tmp_path / "backup/manifest.json"
    edit_json(meta, lambda m: m["aiState"]["verification"].update({field: value}))
    with pytest.raises(ValueError, match="AI recovery verification mismatch"):
        bundle.restore(tmp_path / "backup", tmp_path / "restored", manifest_sha256=bundle.digest(meta))
    assert not (tmp_path / "restored").exists()
