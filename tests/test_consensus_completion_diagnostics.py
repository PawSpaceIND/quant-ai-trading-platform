"""Malformed or unfinished model replies must never become usable signals."""
import asyncio
import copy
import json
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from quant_ai.agents.atlas import AtlasInvestmentAgent
from quant_ai.agents.contracts import AgentDomain, AgentEvidence, Stance
from quant_ai.llm.anthropic_client import AnthropicSwarmClient, ConsensusSchemaError
from quant_ai.llm.budget import SqliteAIBudget


def payload():
    return {"stance": "BUY", "confidence": 0.8, "expected_return": 0.03,
            "expected_risk": 0.02, "rationale": ["synthetic"],
            "xai_proof": {"summary": "synthetic", "supporting_factors": [], "risk_factors": []}}


def client(body=None, stop="tool_use", blocks=None, budget=None):
    block = SimpleNamespace(type="tool_use", name="trading_consensus",
                            input=payload() if body is None else body)
    response = SimpleNamespace(model="synthetic-model", id="synthetic-id", stop_reason=stop,
        usage=SimpleNamespace(input_tokens=80, output_tokens=1200),
        content=[block] if blocks is None else blocks)
    create = AsyncMock(return_value=response)
    return AnthropicSwarmClient(client=SimpleNamespace(messages=SimpleNamespace(create=create)),
                                budget=budget), create


def run(llm):
    return asyncio.run(llm.generate_trading_consensus("synthetic evidence"))


def test_completed_reply_records_safe_termination_details():
    llm, create = client()
    result = run(llm)
    assert result.provenance["stop_reason"] == "tool_use"
    assert result.provenance["content_block_types"] == ["tool_use"]
    assert result.provenance["matching_tool_blocks"] == 1
    assert result.provenance["status"] == "completed"
    assert "failure_code" not in result.provenance
    assert create.await_count == 1
    assert create.await_args.kwargs["max_tokens"] == 1200
    assert "thinking" not in create.await_args.kwargs


@pytest.mark.parametrize("stop,code", [("max_tokens", "output_truncated"),
    ("refusal", "model_refusal"), ("model_context_window_exceeded", "context_limit"),
    ("pause_turn", "incomplete_turn")])
def test_unfinished_reply_refuses_even_if_tool_payload_looks_valid(stop, code):
    llm, create = client(stop=stop)
    with pytest.raises(ConsensusSchemaError) as caught:
        run(llm)
    assert caught.value.provenance["status"] == "invalid_schema"
    assert caught.value.provenance["failure_code"] == code
    assert caught.value.provenance["stop_reason"] == stop
    assert create.await_count == 1


@pytest.mark.parametrize("field,value,code", [
    ("confidence", "0.8", "confidence_not_numeric"),
    ("confidence", True, "confidence_not_numeric"),
    ("confidence", float("nan"), "confidence_nonfinite"),
    ("confidence", 1.1, "confidence_out_of_range"),
    ("expected_risk", -0.1, "negative_expected_risk"),
    ("rationale", [], "rationale_empty"),
    ("stance", "made-up", "unsupported_stance"),
    ("xai_proof", {}, "proof_fields_invalid"),
])
def test_exact_validation_failure_is_recorded_without_payload(field, value, code):
    body = payload()
    body[field] = value
    llm, _ = client(body=body)
    with pytest.raises(ConsensusSchemaError) as caught:
        run(llm)
    assert caught.value.provenance["failure_code"] == code
    assert "response_payload" not in caught.value.provenance


@pytest.mark.parametrize("body,code", [({}, "missing_consensus_fields"),
    ({**payload(), "private-provider-text": "DO_NOT_LOG"}, "unknown_consensus_fields"),
    (["DO_NOT_LOG"], "tool_input_not_object")])
def test_arbitrary_provider_values_do_not_enter_diagnostics(body, code):
    llm, _ = client(body=body)
    with pytest.raises(ConsensusSchemaError) as caught:
        run(llm)
    assert caught.value.provenance["failure_code"] == code
    assert "DO_NOT_LOG" not in json.dumps(caught.value.provenance)
    assert "private-provider-text" not in json.dumps(caught.value.provenance)


@pytest.mark.parametrize("kind,code", [("none", "missing_consensus_tool"),
    ("duplicate", "multiple_tool_blocks"), ("wrong", "unexpected_tool")])
def test_tool_envelope_must_identify_one_consensus(kind, code):
    block = SimpleNamespace(type="tool_use", name="trading_consensus", input=payload())
    blocks = [] if kind == "none" else [block, copy.copy(block)] if kind == "duplicate" else [
        SimpleNamespace(type="tool_use", name="private-invalid-name", input=payload())]
    llm, _ = client(blocks=blocks)
    with pytest.raises(ConsensusSchemaError) as caught:
        run(llm)
    assert caught.value.provenance["failure_code"] == code
    assert "private-invalid-name" not in json.dumps(caught.value.provenance)


def test_invalid_call_still_counts_against_existing_budget(tmp_path):
    budget = SqliteAIBudget(tmp_path / "budget.sqlite", daily_call_limit=1, daily_token_limit=2000)
    llm, create = client(stop="max_tokens", budget=budget)
    with pytest.raises(ConsensusSchemaError):
        run(llm)
    assert budget.status("consensus")["tokens"] == 1280
    assert run(llm).provenance["status"] == "budget_exhausted"
    assert create.await_count == 1
    budget.close()


def test_atlas_abstains_and_preserves_precise_error():
    llm, _ = client(stop="max_tokens")
    now = datetime.now(timezone.utc)
    evidence = tuple(AgentEvidence(str(i), AgentDomain.TECHNICAL, "INFY", Stance.BUY,
        Decimal(".8"), Decimal(".03"), Decimal(".02"), ("synthetic",), now, 0) for i in range(4))
    result = asyncio.run(AtlasInvestmentAgent(llm_client=llm).decide_with_llm("INFY", evidence, now))
    assert result.action == Stance.NEUTRAL and result.confidence == 0
    assert result.provenance["mode"] == "llm_invalid_schema"
    assert result.provenance["inference"]["failure_code"] == "output_truncated"
