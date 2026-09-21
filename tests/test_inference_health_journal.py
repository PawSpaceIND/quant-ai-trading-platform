"""Retain safe decision diagnostics through provider, journal migration and report."""
import asyncio
import json
from dataclasses import replace
from datetime import timedelta

import pytest
from test_consensus_completion_diagnostics import client
from test_decision_journal import SESSION, TENANT, execute, proposal_for, synthetic_row

from quant_ai.analytics import decision_journal as journal
from quant_ai.analytics import decision_quality as quality
from quant_ai.domain.models import Side
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.llm.anthropic_client import ConsensusSchemaError


def persist(tmp_path, provenance):
    broker = PaperBrokerService(tmp_path / "ledger.sqlite")
    result = execute(broker, replace(proposal_for(Side.BUY), provenance=provenance))
    row = journal.decision_row(result, tenant_id=TENANT, now=SESSION)
    journal.insert_decision(broker, row)
    loaded = journal.load_rows(broker, tenant_id=TENANT, since=SESSION - timedelta(seconds=1), until=SESSION + timedelta(seconds=1))
    return row, quality.inference_health(loaded)


def test_real_validation_failure_survives_journal_and_report_without_provider_text(tmp_path):
    llm, _ = client(stop="max_tokens")
    with pytest.raises(ConsensusSchemaError) as caught:
        asyncio.run(llm.generate_trading_consensus("PRIVATE PROMPT"))
    row, health = persist(tmp_path, {"mode": "llm_invalid_schema", "inference": caught.value.provenance})
    assert row["inference_status"] == "invalid_schema"
    assert row["inference_failure_code"] == "output_truncated"
    assert health["failures"] == [{"code": "output_truncated", "decisions": 1}]
    assert "PRIVATE PROMPT" not in json.dumps([row, health])


@pytest.mark.parametrize("status,code", [(401, "provider_auth"), (403, "provider_auth"),
    (429, "provider_rate_limited"), (529, "provider_overloaded"), (503, "provider_unavailable"),
    (None, "provider_unavailable"), ("timeout", "provider_timeout")])
def test_provider_failure_codes_reach_durable_diagnostics(tmp_path, status, code):
    llm, sdk = client()
    error = TimeoutError("PRIVATE FAILURE") if status == "timeout" else RuntimeError("PRIVATE FAILURE")
    if isinstance(status, int):
        error.status_code = status
    sdk.side_effect = error
    response = asyncio.run(llm.generate_trading_consensus("PRIVATE PROMPT"))
    assert response.provenance["failure_code"] == code
    row, health = persist(tmp_path, {"mode": "llm_unavailable", "inference": response.provenance})
    assert row["inference_failure_code"] == code
    assert health["failures"] == [{"code": code, "decisions": 1}]
    assert "PRIVATE" not in json.dumps([row, health])


@pytest.mark.parametrize("status,code,expected", [
    ("invalid_schema", "SECRET unexpected provider field", "invalid_consensus_schema"),
    ("unavailable", {"raw": "SECRET"}, "provider_unavailable"),
    ("completed", "SECRET", None),
    ("budget_exhausted", None, "budget_exhausted"),
    ("unverified", None, "unverified_inference"),
])
def test_unknown_codes_cannot_leak_into_durable_fields(tmp_path, status, code, expected):
    row, health = persist(tmp_path, {"inference": {"status": status, "failure_code": code, "request": "SECRET"}})
    assert row["inference_failure_code"] == expected
    assert "SECRET" not in json.dumps([row, health])


def test_not_requested_is_explicit_and_legacy_null_is_not_inferred(tmp_path):
    row, _ = persist(tmp_path, {"mode": "deterministic"})
    assert row["inference_status"] == "not_requested"
    report = quality.inference_health([row, {"mode": "llm_invalid_schema"},
        {"inference_status": "budget_exhausted"}, {"inference_status": "completed"}])
    assert report["recorded"] == 3 and report["not_recorded"] == 1
    assert report["decisions"] == 4
    assert report["failures"] == [{"code": "budget_exhausted", "decisions": 1}]


def test_old_ledger_migrates_without_inventing_historical_causes(tmp_path):
    broker = PaperBrokerService(tmp_path / "old.sqlite")
    old_schema = journal.SCHEMA.replace("    inference_status TEXT,\n", "").replace("    inference_failure_code TEXT,\n", "")
    with broker._connection as db:
        db.execute(old_schema)
        row = synthetic_row("old")
        columns = [key for key in row if key not in {"inference_status", "inference_failure_code"}]
        db.execute(f"INSERT INTO {journal.TABLE} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", [row[key] for key in columns])
    journal.ensure_journal(broker)
    journal.ensure_journal(broker)
    new = synthetic_row("new", inference_status="invalid_schema", inference_failure_code="proof_summary_empty")
    journal.insert_decision(broker, new)
    report = quality.build_report(broker, tenant_id=TENANT, now=SESSION + timedelta(hours=1))
    health = report["inference_health"]
    assert health["decisions"] == 2 and health["not_recorded"] == 1
    assert health["failures"] == [{"code": "proof_summary_empty", "decisions": 1}]
    with pytest.raises(ValueError, match="not a resolvable journal column"):
        journal.update_decision(broker, "new", inference_failure_code="output_truncated")
