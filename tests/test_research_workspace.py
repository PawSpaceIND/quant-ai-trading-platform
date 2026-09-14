import hashlib
import json
import runpy
import sqlite3
import stat
from pathlib import Path

import pytest

from quant_ai.research.lab import ResearchLab, canonical
from quant_ai.research.workspace_report import publish, workspace_snapshot


@pytest.fixture
def source(tmp_path):
    database = tmp_path / "research.sqlite"
    lab = ResearchLab(database)
    lab.create(
        "review",
        {
            "candidates": ["candidate", "cash"],
            "baseline": "cash",
            "mode": "historical",
            "expected_cases": 5,
            "max_quantity": 10,
            "max_quote_age_seconds": 60,
            "capital_per_case": "1000",
            "fee_bps": "10",
            "slippage_bps": "10",
            "protocol_version": "fixture-v1",
            "private_extra": "PRIVATE_CONFIG_SENTINEL",
        },
    )
    packet = {
        "decision_at": "2026-01-01T04:00:00Z",
        "quote_at": "2026-01-01T04:00:00Z",
        "exchange": "NSE",
        "asset_class": "EQUITY",
        "currency": "INR",
        "symbol": "SYNTHETIC",
        "reference_price": "100",
        "adjustment_status": "unverified",
        "sources": [
            {
                "id": "source",
                "available_at": "2026-01-01T03:59:00Z",
                "provenance": "PRIVATE_SOURCE_SENTINEL",
            }
        ],
    }
    for case in ["loss", "unresolved", "error", "pending", "missing"]:
        lab.add_case("review", case, packet)
        for name in ["candidate", "cash"]:
            if case == "missing" and name == "candidate":
                continue
            lab.record_decision(
                "review",
                case,
                name,
                {
                    "input_digest": hashlib.sha256(canonical(packet).encode()).hexdigest(),
                    "model_version": "fixture-model",
                    "returned_model": "synthetic-returned-model"
                    if name == "candidate" and case != "error"
                    else None,
                    "prompt_version": "fixture-prompt",
                    "decided_at": "2026-01-01T04:00:01Z",
                    "status": "provider_error" if case == "error" and name == "candidate" else "ok",
                    "action": "BUY" if name == "candidate" else "HOLD",
                    "quantity": 10 if name == "candidate" else 0,
                    "rationale": "PRIVATE_RATIONALE_SENTINEL",
                    "evidence_ids": ["source"],
                    "latency_ms": "12",
                    "input_tokens": 3,
                    "output_tokens": 2,
                    "api_cost_usd": None if case == "error" and name == "candidate" else ".001",
                },
            )
        if case in ["loss", "unresolved", "error"]:
            lab.record_outcome(
                "review",
                case,
                {
                    "entry_at": "2026-01-01T04:00:02Z",
                    "exit_at": "2026-01-01T04:01:00Z",
                    "entry_ask": "100",
                    "exit_bid": "99",
                    "entry_available_quantity": 10,
                    "exit_available_quantity": 0 if case == "unresolved" else 10,
                    "provenance": "PRIVATE_EXECUTION_SENTINEL",
                },
            )
    yield lab, database
    lab.close()


def test_workspace_publishes_consistent_allowlisted_evidence_without_source_writes(
    source, tmp_path
):
    lab, database = source
    original = database.read_bytes()
    expected_hash = lab.export_evidence("review")["sha256"]
    output = tmp_path / "review.json"
    envelope = publish(database, "review", "tenant-a", output)
    assert database.read_bytes() == original
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert envelope["sha256"] == hashlib.sha256(envelope["payload"].encode()).hexdigest()
    assert "PRIVATE_" not in output.read_text()
    body = json.loads(envelope["payload"])
    assert body["evidence_sha256"] == expected_hash
    assert body["tenant_id"] == "tenant-a"
    assert body["status"] == "insufficient_evidence"
    assert body["automatic_promotion"] is False
    c = body["candidates"]["candidate"]
    assert [
        c[k]
        for k in (
            "completed_episodes",
            "errors",
            "unresolved_exits",
            "missing_decisions",
            "pending_buy_outcomes",
        )
    ] == [1, 1, 1, 1, 1]
    assert len(c["outcomes"]) == 2
    assert c["unknown_cost_decisions"] == 1 and c["cost_total_complete"] is False
    assert c["returned_models"] == ["synthetic-returned-model"]
    assert body["registered_cases"] == 5 and body["resolved_cases"] == 3
    assert "candidate:unresolved_exits" in body["comparison_blockers"]


def test_workspace_cost_stress_keeps_losses_and_unresolved_exits(source):
    lab, _ = source
    body = json.loads(workspace_snapshot(lab, "review", "default")["payload"])
    stresses = body["candidates"]["candidate"]["cost_stress"]
    assert all(s["completed_cases"] == 1 and s["unresolved_exits"] == 1 for s in stresses)
    assert (
        float(stresses[2]["completed_case_pnl_inr"])
        < float(stresses[0]["completed_case_pnl_inr"])
        < 0
    )
    assert all(s["completed_cases"] == 0 for s in body["candidates"]["cash"]["cost_stress"])


def test_publish_never_overwrites_existing_file_or_research_database(source, tmp_path):
    _, database = source
    output = tmp_path / "already.json"
    output.write_text("keep")
    for target in [output, database]:
        before = target.read_bytes()
        with pytest.raises(FileExistsError):
            publish(database, "review", "default", target)
        assert target.read_bytes() == before


def test_missing_wrong_or_tampered_source_produces_no_report(source, tmp_path):
    lab, database = source
    output = tmp_path / "output.json"
    missing = tmp_path / "missing.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        publish(missing, "review", "default", output)
    assert not missing.exists() and not output.exists()
    wrong = tmp_path / "paper.sqlite"
    db = sqlite3.connect(wrong)
    db.execute("CREATE TABLE paper_accounts(cash TEXT)")
    db.close()
    with pytest.raises(ValueError, match="not_a_research_database"):
        publish(wrong, "review", "default", output)
    with lab.db:
        lab.db.execute("UPDATE cases SET packet='{}' WHERE id='loss'")
    with pytest.raises(ValueError, match="packet_integrity_failure"):
        publish(database, "review", "default", output)
    assert not output.exists()


def test_cloud_publisher_does_not_transmit_lab_report(monkeypatch, tmp_path):
    namespace = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "scripts/publish_cloud_snapshot.py")
    )
    config = tmp_path / "cloud.json"
    config.write_text(
        json.dumps({"url": "https://synthetic.invalid", "publish_token": "test-only"})
    )
    monkeypatch.setitem(namespace["publish"].__globals__, "CONFIG", config)
    monkeypatch.delenv("PRAMANA_SOURCE_DASHBOARD_SECRET", raising=False)

    class Result:
        status_code = 200

        def __init__(self, value=None):
            self.value = value

        def raise_for_status(self):
            pass

        def json(self):
            return self.value

    class Client:
        def get(self, url, **kwargs):
            if url.endswith("/workspace"):
                return Result(
                    {
                        "tenantId": "india-paper",
                        "portfolio": {},
                        "researchLab": {"private": "RESEARCH_SENTINEL"},
                        "researchPortfolio": {"private": "PORTFOLIO_SENTINEL"},
                        "paperContribution": {"private": "CONTRIBUTION_SENTINEL"},
                        "historicalRisk": {"private": "RISK_SENTINEL"},
                        "companyEvents": {"private": "EVENT_SENTINEL"},
                        "audit": ["NOTES_SENTINEL"],
                    }
                )
            return Result({"tenantId": "india-paper"})

    sent = []

    def send(*args, **kwargs):
        sent.append(kwargs["json"])
        return Result()

    monkeypatch.setattr(namespace["requests"], "Session", Client)
    monkeypatch.setattr(namespace["requests"], "post", send)
    namespace["publish"]()
    assert len(sent) == 1
    workspace = sent[0]["snapshots"]["/api/workspace"]
    assert "researchLab" not in workspace and workspace["audit"] == []
    assert "researchPortfolio" not in workspace
    assert "paperContribution" not in workspace
    assert "historicalRisk" not in workspace
    assert "companyEvents" not in workspace
    assert "SENTINEL" not in json.dumps(sent)
