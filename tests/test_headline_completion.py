"""Offline headline completion, strict payload, usage and scorer/cache regressions."""
from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import quant_ai.llm.anthropic_client as adapter
from quant_ai.intelligence.headline_sentiment import (
    KEYWORD_SCORER,
    MODEL_SCORER,
    HeadlineSentimentScorer,
    keyword_sentiment,
)
from quant_ai.llm.budget import SqliteAIBudget
from quant_ai.llm.provenance import content_hash

SUBJECT = "TEST"
HEADLINE = "Synthetic market rally on peace deal"
STOP_CASES = [
    pytest.param("max_tokens", "output_truncated", id="truncated"),
    pytest.param("refusal", "model_refusal", id="refused"),
    pytest.param("pause_turn", "incomplete_turn", id="paused"),
    pytest.param("model_context_window_exceeded", "context_limit", id="context"),
    pytest.param(None, "completion_unverified", id="missing"),
    pytest.param("end_turn", "completion_unverified", id="end_turn"),
    pytest.param("stop_sequence", "completion_unverified", id="stop_sequence"),
    pytest.param("private-provider-value", "completion_unverified", id="unknown"),
]


def payload():
    return {"scores": [{"index": 0, "sentiment": 0.5, "rationale": "Synthetic supplied evidence"}]}


def tool(name=adapter.HEADLINE_TOOL_NAME, value=None):
    return SimpleNamespace(type="tool_use", name=name, input=payload() if value is None else value)


def reply(stop="tool_use"):
    return SimpleNamespace(stop_reason=stop, content=[tool()], model="synthetic-model",
                           id="synthetic-response", usage=SimpleNamespace(input_tokens=11, output_tokens=17))


def envelope(kind):
    result = reply()
    if kind == "missing":
        result.content = []
    elif kind == "wrong":
        result.content = [tool("private-wrong-tool")]
    elif kind == "duplicate":
        result.content = [tool(), tool()]
    elif kind == "mixed":
        result.content = [tool(), tool("private-other-tool")]
    elif kind == "nonobject":
        result.content = [tool(value=[])]
    elif kind == "null_content":
        result.content = None
    elif kind == "string_content":
        result.content = "private-content"
    elif kind == "text_only":
        result.content = [SimpleNamespace(type="text", text="private-text")]
    elif kind == "null_input":
        result.content[0].input = None
    else:
        raise AssertionError(kind)
    return result


ENVELOPE_CASES = [
    pytest.param("missing", "missing_headline_tool", id="missing"),
    pytest.param("wrong", "unexpected_headline_tool", id="wrong"),
    pytest.param("duplicate", "multiple_tool_blocks", id="duplicate"),
    pytest.param("mixed", "multiple_tool_blocks", id="mixed"),
    pytest.param("nonobject", "tool_input_not_object", id="nonobject"),
    pytest.param("null_content", "missing_headline_tool", id="null_content"),
    pytest.param("string_content", "missing_headline_tool", id="string_content"),
    pytest.param("text_only", "missing_headline_tool", id="text_only"),
    pytest.param("null_input", "tool_input_not_object", id="null_input"),
]


@pytest.fixture
def client_factory(monkeypatch):
    def make(responses, *, budget=None, injected=False):
        create = AsyncMock(side_effect=responses)
        transport = SimpleNamespace(messages=SimpleNamespace(create=create))
        monkeypatch.setattr(adapter, "AsyncAnthropic", lambda **kwargs: transport)
        client = adapter.AnthropicSwarmClient(
            client=transport if injected else None, api_key="synthetic-test-key",
            model="synthetic-model", budget=budget,
        )
        assert client.transport_kind == ("injected_client" if injected else "anthropic_sdk")
        return client, create
    return make


def invoke(client):
    return asyncio.run(client.score_headlines(SUBJECT, (HEADLINE,)))


def assert_invalid(result, code):
    assert result == {"scores": []}
    assert result.provenance["status"] == "invalid_schema"
    assert result.provenance["failure_code"] == code
    assert "response_payload_sha256" not in result.provenance


@pytest.mark.parametrize("stop,code", STOP_CASES)
def test_sdk_termination_must_be_completed_tool(client_factory, stop, code):
    client, create = client_factory([reply(stop)])
    result = invoke(client)
    assert_invalid(result, code)
    assert result.provenance["stop_reason"] == ("unrecognized" if stop == "private-provider-value" else stop)
    assert result.provenance["matching_tool_blocks"] == 1
    assert create.await_count == 1


@pytest.mark.parametrize("stop,code", STOP_CASES[:4])
def test_explicit_unfinished_injected_responses_also_refuse(client_factory, stop, code):
    client, _ = client_factory([reply(stop)], injected=True)
    assert_invalid(invoke(client), code)


@pytest.mark.parametrize("kind,code", ENVELOPE_CASES)
def test_envelope_requires_one_correct_object_tool(client_factory, kind, code):
    client, create = client_factory([envelope(kind)])
    assert_invalid(invoke(client), code)
    assert create.await_count == 1


@pytest.mark.parametrize("stop,code", STOP_CASES)
def test_unfinished_result_is_not_model_evidence_or_cached(client_factory, stop, code):
    client, create = client_factory([reply(stop), reply()])
    scorer = HeadlineSentimentScorer(client)
    first = asyncio.run(scorer.score(SUBJECT, (HEADLINE,)))
    assert first[0].scorer == KEYWORD_SCORER
    assert first[0].sentiment == keyword_sentiment(HEADLINE)
    assert first[0].rationale == "keyword_fallback:invalid_schema"
    assert not scorer._cache and create.await_count == 1
    second = asyncio.run(scorer.score(SUBJECT, (HEADLINE,)))
    assert second[0].scorer == MODEL_SCORER and create.await_count == 2
    third = asyncio.run(scorer.score(SUBJECT, (HEADLINE,)))
    assert third == second and create.await_count == 2 and scorer.cache_hits == 1


@pytest.mark.parametrize("kind,code", ENVELOPE_CASES)
def test_invalid_envelope_never_populates_model_cache(client_factory, kind, code):
    client, create = client_factory([envelope(kind)])
    scorer = HeadlineSentimentScorer(client)
    result = asyncio.run(scorer.score(SUBJECT, (HEADLINE,)))
    assert result[0].scorer == KEYWORD_SCORER
    assert not scorer._cache and create.await_count == 1


def test_complete_reply_has_real_completion_and_payload_identity(client_factory):
    response = reply()
    response.content.insert(0, SimpleNamespace(type="text", text="private text"))
    client, _ = client_factory([response])
    result = invoke(client)
    assert result == payload()
    assert result.provenance["status"] == "completed"
    assert result.provenance["stop_reason"] == "tool_use"
    assert result.provenance["matching_tool_blocks"] == 1
    assert result.provenance["content_block_count"] == 2
    assert result.provenance["response_payload_sha256"] == content_hash(payload())
    assert "failure_code" not in result.provenance


def test_legacy_injected_metadata_stays_explicitly_unknown(client_factory):
    client, _ = client_factory([reply(None)], injected=True)
    result = invoke(client)
    assert result == payload() and result.provenance["status"] == "completed"
    assert result.provenance["transport"] == "injected_client"
    assert result.provenance["stop_reason"] is None


BAD_PAYLOADS = [
    pytest.param({"scores": []}, id="empty"),
    pytest.param({"scores": [{"index": 0, "sentiment": 2, "rationale": "x"}]}, id="range"),
    pytest.param({"scores": [{"index": 0, "sentiment": True, "rationale": "x"}]}, id="bool"),
    pytest.param({"scores": [{"index": 0, "sentiment": "0.5", "rationale": "x"}]}, id="text"),
    pytest.param({"scores": [{"index": 0, "sentiment": float("nan"), "rationale": "x"}]}, id="nan"),
    pytest.param({"scores": [{"index": 0, "sentiment": float("inf"), "rationale": "x"}]}, id="infinite"),
    pytest.param({"scores": [{"index": True, "sentiment": 0.5, "rationale": "x"}]}, id="bool_index"),
    pytest.param({"scores": [{"index": 1, "sentiment": 0.5, "rationale": "x"}]}, id="bad_index"),
    pytest.param({"scores": [{"index": 0, "sentiment": 0.5, "rationale": " "}]}, id="empty_rationale"),
    pytest.param({"scores": [{"index": 0, "sentiment": 0.5}]}, id="missing_rationale"),
    pytest.param({"scores": [{"index": 0, "sentiment": 0.5, "rationale": "x"}], "private-key": "private-value"}, id="unknown"),
]


@pytest.mark.parametrize("bad", BAD_PAYLOADS)
def test_adapter_validates_existing_strict_schema_before_completion(client_factory, bad):
    response = reply()
    response.content[0].input = copy.deepcopy(bad)
    client, _ = client_factory([response])
    assert_invalid(invoke(client), "invalid_headline_schema")


def test_parser_is_not_called_on_nonobject_input(client_factory, monkeypatch):
    client, _ = client_factory([envelope("nonobject")])
    parser = Mock()
    monkeypatch.setattr(client, "parse_headline_scores", parser)
    assert_invalid(invoke(client), "tool_input_not_object")
    parser.assert_not_called()


def test_diagnostics_omit_provider_text_and_bound_metadata(client_factory):
    response = reply("private-provider-stop")
    response.content = [SimpleNamespace(type="private-block-type", text="private-block-text") for _ in range(40)] + [tool()]
    client, _ = client_factory([response])
    result = invoke(client)
    assert_invalid(result, "completion_unverified")
    meta = result.provenance
    assert meta["stop_reason"] == "unrecognized"
    assert meta["content_block_types"] == ["other"] * 32
    assert meta["content_block_count"] == 41 and meta["matching_tool_blocks"] == 1
    assert "private-" not in json.dumps(meta)


def test_schema_error_does_not_echo_provider_values(client_factory, monkeypatch):
    client, _ = client_factory([reply()])
    monkeypatch.setattr(client, "parse_headline_scores", Mock(side_effect=adapter.ConsensusSchemaError("private-field private-value")))
    result = invoke(client)
    assert_invalid(result, "invalid_headline_schema")
    assert "private-" not in json.dumps(result.provenance)


def test_spent_usage_is_recorded_before_rejection_without_extra_request(client_factory, tmp_path):
    budget = SqliteAIBudget(tmp_path / "budget.sqlite", daily_call_limit=1, daily_token_limit=10000)
    try:
        client, create = client_factory([reply("max_tokens")], budget=budget)
        assert_invalid(invoke(client), "output_truncated")
        status = budget.status(adapter.HEADLINE_BUDGET_SCOPE)
        assert status["calls"] == 1 and status["input_tokens"] == 11 and status["output_tokens"] == 17
        assert budget.status(adapter.BUDGET_SCOPE)["calls"] == 0
        exhausted = invoke(client)
        assert exhausted.provenance["status"] == "budget_exhausted" and create.await_count == 1
    finally:
        budget.close()


def test_budget_exhaustion_prevents_the_first_sdk_request(client_factory, tmp_path):
    budget = SqliteAIBudget(tmp_path / "budget.sqlite", daily_call_limit=1, daily_token_limit=10000)
    try:
        assert budget.reserve(adapter.HEADLINE_BUDGET_SCOPE)
        client, create = client_factory([], budget=budget)
        assert invoke(client).provenance["status"] == "budget_exhausted"
        create.assert_not_called()
    finally:
        budget.close()


def test_malformed_metadata_refuses_after_usage_recording(client_factory, tmp_path):
    budget = SqliteAIBudget(tmp_path / "budget.sqlite", daily_call_limit=2, daily_token_limit=10000)
    try:
        response = reply()
        response.content.append(SimpleNamespace(type=[]))
        client, create = client_factory([response], budget=budget)
        assert_invalid(invoke(client), "invalid_response_metadata")
        assert budget.status(adapter.HEADLINE_BUDGET_SCOPE)["tokens"] == 28
        assert create.await_count == 1
    finally:
        budget.close()


def test_headline_request_profile_does_not_follow_consensus_cap(client_factory, monkeypatch):
    monkeypatch.setenv(adapter.CONSENSUS_MAX_TOKENS_ENV, "4096")
    client, create = client_factory([reply()])
    assert client.consensus_max_tokens == 4096
    result = invoke(client)
    request = create.await_args.kwargs
    assert request["max_tokens"] == 1000
    assert request["tools"][0]["input_schema"] == adapter._headline_schema(1)
    assert request["system"] == adapter.HEADLINE_SYSTEM
    assert result.provenance["request_sha256"] == content_hash(request)
    assert result.provenance["timeout_seconds"] == 30.0
