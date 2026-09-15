from datetime import datetime, timezone

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

    missing = evidence()
    del missing["gates"]["X01"]["coverage"]
    assert not assess_external_gates(missing)[0].passed


def test_missing_or_invalid_external_document_never_claims_readiness():
    report = external_gate_report({"revision": "", "targetHost": "", "gates": {}})
    assert report["ready"] is False
    assert all(not item["passed"] for item in report["gates"])
