import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from quant_ai.operations.pilot_gate import (
    EXTERNAL_GATES,
    assess_external_gates,
    external_gate_report,
)


def evidence(status="passed"):
    now = datetime.now(timezone.utc).isoformat()
    return {
        "revision": "a" * 40,
        "targetHost": "pilot-host",
        "gates": {
            gate.gate_id: {
                "status": status,
                "reviewer": "founder-reviewer",
                "observedAt": now,
                "evidence": [f"evidence/{gate.gate_id}.json"],
                "evidenceSha256": "b" * 64,
                **{item: True for item in gate.required_evidence},
            }
            for gate in EXTERNAL_GATES
        },
    }


def test_external_gates_require_every_attached_reviewed_evidence_item():
    report = external_gate_report(evidence())
    assert report["ready"] is True
    assert report["liveExecutionEnabled"] is False
    assert report["revision"] == "a" * 40
    assert report["targetHost"] == "pilot-host"
    assert all(item["passed"] for item in report["gates"])
    assert all(item["evidenceSha256"] == "b" * 64 for item in report["gates"])

    missing = evidence()
    del missing["gates"]["X01"]["coverage"]
    assert not assess_external_gates(missing)[0].passed

    bad_digest = evidence()
    bad_digest["gates"]["X01"]["evidenceSha256"] = "unbound"
    assert not assess_external_gates(bad_digest)[0].passed

    bad_revision = evidence()
    bad_revision["revision"] = "not-a-sha"
    assert not all(item.passed for item in assess_external_gates(bad_revision))


def test_missing_or_invalid_external_document_never_claims_readiness():
    report = external_gate_report({"revision": "", "targetHost": "", "gates": {}})
    assert report["ready"] is False
    assert all(not item["passed"] for item in report["gates"])


def test_cli_publishes_private_report_without_overwriting_review(tmp_path):
    source = tmp_path / "review.json"
    source.write_text(json.dumps(evidence()))
    destination = tmp_path / "report.json"
    command = [sys.executable, str(Path(__file__).resolve().parents[1] / "scripts/pilot_ops.py"),
               "pilot-check", "--evidence", str(source), "--destination", str(destination)]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(destination.read_text()) == json.loads(result.stdout)
    assert destination.stat().st_mode & 0o777 == 0o600
    original = destination.read_bytes()
    assert subprocess.run(command, check=False, capture_output=True).returncode != 0
    assert destination.read_bytes() == original
    source.write_text(json.dumps({"gates": {}}))
    command[-1] = str(tmp_path / "pending.json")
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    assert result.returncode == 2
    assert json.loads(Path(command[-1]).read_text())["ready"] is False
