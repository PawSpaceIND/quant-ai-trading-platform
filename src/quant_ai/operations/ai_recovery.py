"""Read-only recovery of explicitly selected AI registry and training evidence.

Opaque artifacts are hashed, never imported or deserialized. Stored paper approvals
are replayed as history only; capture/restore cannot approve or activate a model.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path

from quant_ai.learning import candidates as registry
from quant_ai.learning.contracts import TrainingDatasetManifest, TrainingRunManifest
from quant_ai.learning.training import training_dataset_digest

SCHEMA = "pramana.ai_recovery_capture.v1"
ROOT = "ai-state"
MAX_CANDIDATES = 128
MAX_MANIFEST_BYTES = 65536
MAX_ARTIFACT_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
ROLES = {"run_manifest": "run.json", "dataset_manifest": "dataset.json", "artifact": "artifact.bin"}


def _check(condition, reason):
    if not condition:
        raise ValueError("AI recovery " + reason)


def _identity(value):
    _check(isinstance(value, str) and 0 < len(value) <= 256 and value == value.strip()
           and all(char.isprintable() for char in value), "identity invalid")


def candidate_paths(candidate_id):
    _identity(candidate_id)
    prefix = ROOT + "/" + hashlib.sha256(candidate_id.encode()).hexdigest()
    return {role: prefix + "/" + name for role, name in ROLES.items()}


def _regular(path, limit):
    _check(not path.is_symlink() and path.is_file(), "existing regular file required")
    info = path.stat()
    _check(info.st_nlink == 1, "file alias unsupported")
    _check(info.st_size <= limit, "file size limit")
    return info.st_size


def _file(path, limit, *, contents=False):
    _regular(path, limit)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    digest, parts, count = hashlib.sha256(), [], 0
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        _check(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "file alias unsupported")
        _check(info.st_size <= limit, "file size limit")
        while chunk := stream.read(min(1024 * 1024, limit + 1 - count)):
            count += len(chunk)
            _check(count <= limit, "file size limit")
            digest.update(chunk)
            if contents:
                parts.append(chunk)
        _check(count == info.st_size, "file changed during read")
    return b"".join(parts) if contents else digest.hexdigest()


def selection(value, tenant):
    """Paths are declared by the operator; tenant selection is not registry authentication."""
    _identity(tenant)
    _check(isinstance(value, dict) and set(value) == {"tenant", "registry", "candidates"},
           "selection fields invalid")
    _check(value["tenant"] == tenant, "tenant declaration mismatch")
    selected = value["candidates"]
    _check(isinstance(selected, dict) and len(selected) <= MAX_CANDIDATES, "candidate inventory invalid")
    paths = {}
    declared = {"tenant": tenant, "registryPath": ROOT + "/registry.jsonl", "candidates": {}}
    def add(role, raw, limit):
        _check(isinstance(raw, str) and bool(raw.strip()) and raw == raw.strip() and raw != ":memory:",
               "explicit file path required")
        path = Path(raw).absolute()
        _regular(path, limit)
        paths[role] = path
    add(declared["registryPath"], value["registry"], registry.MAX_REGISTRY_BYTES)
    for candidate_id, entry in selected.items():
        names = candidate_paths(candidate_id)
        _check(isinstance(entry, dict) and set(entry) == set(ROLES), "candidate files required")
        declared["candidates"][candidate_id] = names
        for role, name in names.items():
            add(name, entry[role], MAX_ARTIFACT_BYTES if role == "artifact" else MAX_MANIFEST_BYTES)
    _check(sum(p.stat().st_size for p in paths.values()) <= MAX_TOTAL_BYTES, "total size limit")
    return paths, declared


def validate_declaration(value, tenant):
    _identity(tenant)
    _check(isinstance(value, dict) and set(value) == {"tenant", "registryPath", "candidates"},
           "declaration invalid")
    _check(value["tenant"] == tenant and value["registryPath"] == ROOT + "/registry.jsonl",
           "tenant or registry declaration invalid")
    selected = value["candidates"]
    _check(isinstance(selected, dict) and len(selected) <= MAX_CANDIDATES, "candidate inventory invalid")
    for candidate_id, entry in selected.items():
        _check(entry == candidate_paths(candidate_id), "candidate path declaration invalid")
    return {value["registryPath"]} | {path for entry in selected.values() for path in entry.values()}


def validate_capture(value, tenant, files):
    _check(isinstance(value, dict) and set(value) == {"selection", "verification"}
           and isinstance(value["verification"], dict), "capture inventory invalid")
    selected = validate_declaration(value["selection"], tenant)
    actual = {name for name in files if name == ROOT or name.startswith(ROOT + "/")}
    _check(selected == actual, "capture file inventory mismatch")
    total = 0
    for name in selected:
        info = files[name]
        limit = (registry.MAX_REGISTRY_BYTES if name == value["selection"]["registryPath"]
                 else MAX_ARTIFACT_BYTES if name.endswith("/artifact.bin") else MAX_MANIFEST_BYTES)
        _check(isinstance(info, dict) and set(info) == {"size", "sha256"}
               and type(info["size"]) is int and 0 <= info["size"] <= limit,
               "capture size limit")
        total += info["size"]
    _check(total <= MAX_TOTAL_BYTES, "capture total size limit")


def _json(path):
    try:
        value = json.loads(_file(path, MAX_MANIFEST_BYTES, contents=True).decode("utf-8"),
            object_pairs_hook=registry._unique_keys, parse_constant=registry._nonfinite_json)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise ValueError("AI recovery manifest JSON invalid") from error
    _check(isinstance(value, dict), "manifest JSON invalid")
    return value


def _moment(raw):
    try:
        value = datetime.fromisoformat(raw)
        _check(value.tzinfo is not None and value.utcoffset() is not None, "aware timestamp required")
        return value.astimezone(timezone.utc)
    except (TypeError, ValueError) as error:
        raise ValueError("AI recovery aware timestamp required") from error


def _manifest(path, cls, time_field):
    values = _json(path)
    _check(set(values) == {field.name for field in fields(cls)}, "manifest fields invalid")
    values[time_field] = _moment(values[time_field])
    try:
        return cls(**values)
    except (TypeError, ValueError, ArithmeticError, AttributeError) as error:
        raise ValueError("AI recovery manifest contract invalid") from error


def inspect(root: Path, declared: dict, *, tenant: str, as_of: str) -> dict:
    """Revalidate captured bytes, declared training contracts and complete candidate history."""
    validate_declaration(declared, tenant)
    moment = _moment(as_of)
    path = root / declared["registryPath"]
    _regular(path, registry.MAX_REGISTRY_BYTES)
    with registry._registry_lock(path, writing=False) as locked:
        _check(locked is not None, "registry missing")
        records = registry._read_registry_records(locked)
        _check(all(set(r) == {"schema", "sequence", "recorded_at", "event_type", "payload",
                             "previous_sha256", "sha256"}
                   and type(r["sequence"]) is int and r["event_type"] == registry.EVENT for r in records),
               "registry event inventory invalid")
        stages, last_time = registry._replay(records)
        _check(last_time is None or last_time <= moment, "registry evidence from future")
        registry_sha = _file(path, registry.MAX_REGISTRY_BYTES)
    _check(set(stages) == set(declared["candidates"]), "candidate inventory mismatch")
    earliest = {}
    for record in records:
        earliest.setdefault(record["payload"]["candidate_id"], _moment(record["recorded_at"]))
    checked, run_ids = {}, set()
    for candidate_id, names in sorted(declared["candidates"].items()):
        run = _manifest(root / names["run_manifest"], TrainingRunManifest, "trained_at")
        dataset = _manifest(root / names["dataset_manifest"], TrainingDatasetManifest, "cutoff")
        _check(run.candidate_id == candidate_id and run.dataset_id == dataset.dataset_id,
               "training identity mismatch")
        _check(run.run_id not in run_ids, "duplicate run identity")
        run_ids.add(run.run_id)
        _check(dataset.cutoff <= run.trained_at <= earliest[candidate_id] <= moment,
               "training timeline invalid")
        _check(run.dataset_manifest_sha256 is not None, "legacy dataset binding unavailable")
        _check(run.dataset_manifest_sha256 == training_dataset_digest(dataset), "dataset binding mismatch")
        artifact_sha = _file(root / names["artifact"], MAX_ARTIFACT_BYTES)
        _check((root / names["artifact"]).stat().st_size > 0, "empty artifact")
        _check(artifact_sha == run.artifact_sha256, "artifact checksum mismatch")
        checked[candidate_id] = {"runId": run.run_id, "artifactSha256": artifact_sha,
            "datasetManifestSha256": run.dataset_manifest_sha256,
            "runFileSha256": _file(root / names["run_manifest"], MAX_MANIFEST_BYTES),
            "datasetFileSha256": _file(root / names["dataset_manifest"], MAX_MANIFEST_BYTES)}
    return {"schema": SCHEMA, "status": "selected_state_verified", "tenant": tenant,
        "registrySha256": registry_sha, "registryRecords": len(records),
        "recordedStages": {key: value.value for key, value in sorted(stages.items())},
        "candidates": checked, "activationAuthorized": False, "liveExecutionAuthorized": False,
        "artifactApprovalBindingVerified": False, "datasetBytesVerified": False,
        "performanceEvidenceVerified": False, "reviewerAuthenticated": False,
        "scope": "Explicitly selected persisted registry and training evidence only. Paper stages are historical; artifacts are never loaded. Tenant and run selection are operator declarations, not authenticated deployment or approval-to-artifact bindings."}
