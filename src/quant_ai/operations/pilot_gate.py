"""Fail-closed external evidence gate for paper-pilot acceptance."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class ExternalGate:
    gate_id: str
    title: str
    required_evidence: tuple[str, ...]


@dataclass(frozen=True)
class GateResult:
    gate: ExternalGate
    passed: bool
    detail: str


EXTERNAL_GATES = (
    ExternalGate(
        "X01",
        "Real-feed session observation",
        ("open_session_feed", "source_timestamps", "coverage", "portfolio_parity", "independent_protection"),
    ),
    ExternalGate(
        "X02",
        "Sustained operational burn-in",
        ("target_host", "token_renewal", "independent_alert", "soak", "restore_rollback"),
    ),
    ExternalGate(
        "X03",
        "Strategy effectiveness",
        ("ai_holdout", "forward_paper", "after_costs", "calibration_drift", "reviewed_metrics"),
    ),
)


def _aware_timestamp(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def evidence_bundle_digest(paths: list[str], root: Path) -> str:
    """Hash a canonical path/content manifest rooted beside the review document."""
    base = root.resolve(strict=True)
    manifest = []
    if len(set(paths)) != len(paths):
        raise ValueError("duplicate evidence path")
    for name in sorted(paths):
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("evidence path must stay within review directory")
        target = (base / relative).resolve(strict=True)
        if not target.is_relative_to(base) or not target.is_file():
            raise ValueError("evidence must be a file within review directory")
        digest = hashlib.sha256()
        with target.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        manifest.append({"path": name, "sha256": digest.hexdigest()})
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def assess_external_gates(document: Mapping[str, object], *, evidence_root: Path | None = None) -> tuple[GateResult, ...]:
    """Assess a signed-off evidence document without granting any execution permission."""
    revision = document.get("revision")
    host = document.get("targetHost")
    gates = document.get("gates")
    results: list[GateResult] = []
    for gate in EXTERNAL_GATES:
        item = gates.get(gate.gate_id) if isinstance(gates, Mapping) else None
        if not isinstance(item, Mapping):
            results.append(GateResult(gate, False, "missing gate evidence"))
            continue
        missing = [name for name in gate.required_evidence if item.get(name) not in (True, "passed")]
        attachments = item.get("evidence")
        if not isinstance(attachments, list) or not attachments or not all(isinstance(path, str) and path for path in attachments):
            missing.append("evidence_attachments")
        digest = item.get("evidenceSha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            missing.append("evidenceSha256")
        if evidence_root is None:
            missing.append("evidence_root")
        elif "evidence_attachments" not in missing and "evidenceSha256" not in missing:
            try:
                if evidence_bundle_digest(attachments, evidence_root) != digest.lower():
                    missing.append("evidence_digest_mismatch")
            except (OSError, ValueError, RuntimeError):
                missing.append("evidence_unreadable_or_unsafe")
        if not isinstance(item.get("reviewer"), str) or not item["reviewer"].strip():
            missing.append("reviewer")
        if not _aware_timestamp(item.get("observedAt")):
            missing.append("observedAt")
        if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", revision):
            missing.append("revision")
        if not isinstance(host, str) or not host.strip():
            missing.append("targetHost")
        detail = "passed" if not missing and item.get("status") == "passed" else "missing or invalid: " + ", ".join(dict.fromkeys(missing + ([] if item.get("status") == "passed" else ["status"])))
        results.append(GateResult(gate, detail == "passed", detail))
    return tuple(results)


def external_gate_report(document: Mapping[str, object], *, evidence_root: Path | None = None) -> dict[str, object]:
    results = assess_external_gates(document, evidence_root=evidence_root)
    source_gates = document.get("gates")
    return {
        "schema": "pramana.external_gate_report.v2",
        "ready": bool(results) and all(result.passed for result in results),
        "liveExecutionEnabled": False,
        "revision": document.get("revision"),
        "targetHost": document.get("targetHost"),
        "gates": [
            {"id": result.gate.gate_id, "title": result.gate.title, "passed": result.passed, "detail": result.detail,
             "evidenceSha256": (source_gates.get(result.gate.gate_id, {}).get("evidenceSha256")
                                 if isinstance(source_gates, Mapping) and isinstance(source_gates.get(result.gate.gate_id), Mapping)
                                 else None)}
            for result in results
        ],
    }
