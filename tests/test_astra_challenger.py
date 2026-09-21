from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from test_consensus_strict_tool import payload, sdk
from test_regime_playbooks import MODERATE, NOW

from quant_ai.agents.atlas import AtlasInvestmentAgent
from quant_ai.llm.anthropic_client import AnthropicSwarmClient, ConsensusSchemaError
from quant_ai.llm.budget import SqliteAIBudget
from quant_ai.llm.challenger import ChallengerConsensusClient, with_astra_challenger
from quant_ai.llm.openai_client import ASTRA_MODEL, OpenAIConsensusClient
from quant_ai.llm.provenance import content_hash


def response(body=None, **overrides):
    result = {
        "status": "completed", "model": ASTRA_MODEL, "id": "resp_synthetic",
        "usage": {"input_tokens": 100, "output_tokens": 200,
                  "input_tokens_details": {"cached_tokens": 40}},
        "output": [{"type": "reasoning", "summary": []}, {
            "type": "message", "status": "completed", "role": "assistant",
            "content": [{"type": "output_text", "text": json.dumps(body or payload())}],
        }],
    }
    return {**result, **overrides}


def client(body=None, *, status=200, handler=None, **kwargs):
    requests = []
    async def send(request):
        requests.append(request)
        if handler:
            return await handler(request)
        return httpx.Response(status, json=body if body is not None else response())
    result = OpenAIConsensusClient(api_key="test-secret", transport=httpx.MockTransport(send), **kwargs)
    return result, requests


def test_responses_request_is_strict_bounded_and_does_not_store_or_leak_credentials(tmp_path):
    budget = SqliteAIBudget(tmp_path / "budget", daily_call_limit=2, daily_token_limit=100_000)
    model, requests = client(budget=budget)
    result = asyncio.run(model.generate_trading_consensus("same evidence"))
    request = json.loads(requests[0].content)
    assert str(requests[0].url) == "https://api.openai.com/v1/responses"
    assert request["model"] == ASTRA_MODEL and request["store"] is False
    assert request["text"]["format"]["strict"] is True
    assert request["max_output_tokens"] == 8192
    assert request["reasoning"] == {"effort": "medium"}
    assert not {"temperature", "top_p", "tools"} & request.keys()
    assert result.provenance["prompt_sha256"] == content_hash("same evidence")
    assert result.provenance["status"] == "completed"
    assert model.parse_consensus(result)[1].model == ASTRA_MODEL
    assert "test-secret" not in str(result.provenance)
    usage = budget.status("astra_consensus")
    assert usage["input_tokens"] == 100 and usage["output_tokens"] == 200
    assert usage["reserved_tokens"] == 0


@pytest.mark.parametrize("mutation,code", [
    ({"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}, "output_truncated"),
    ({"status": "in_progress"}, "incomplete_turn"),
    ({"model": None}, "completion_unverified"),
    ({"output": []}, "invalid_consensus_schema"),
    ({"output": [{"type": "function_call"}]}, "unexpected_tool"),
    ({"output": [{"type": "message", "role": "assistant", "status": "completed",
                  "content": [{"type": "refusal", "refusal": "SECRET RESPONSE"}]}]}, "model_refusal"),
])
def test_incomplete_refused_or_unverified_responses_never_become_consensus(mutation, code):
    model, _ = client(response(**mutation))
    with pytest.raises(ConsensusSchemaError) as caught:
        asyncio.run(model.generate_trading_consensus("evidence"))
    assert caught.value.provenance["failure_code"] == code
    assert "SECRET RESPONSE" not in str(caught.value.provenance)


@pytest.mark.parametrize("body,code", [
    (payload(confidence=1.2), "confidence_out_of_range"),
    (payload(confidence=True), "confidence_not_numeric"),
    (payload(expected_risk=-1), "negative_expected_risk"),
    (payload(expected_return=float("nan")), "expected_return_nonfinite"),
    (payload(extra="SECRET RESPONSE"), "unknown_consensus_fields"),
    (payload(rationale="bad"), "rationale_not_string_array"),
])
def test_astra_uses_the_same_strict_numeric_and_shape_validation(body, code):
    model, _ = client(response(body))
    with pytest.raises(ConsensusSchemaError) as caught:
        asyncio.run(model.generate_trading_consensus("evidence"))
    assert caught.value.provenance["failure_code"] == code
    assert "SECRET RESPONSE" not in str(caught.value.provenance)


@pytest.mark.parametrize("status,code", [(401, "provider_auth"), (403, "provider_auth"),
    (429, "provider_rate_limited"), (503, "provider_overloaded"), (500, "provider_unavailable")])
def test_provider_errors_are_neutral_and_not_retried(status, code):
    model, requests = client({"error": "SECRET BODY"}, status=status)
    result = asyncio.run(model.generate_trading_consensus("evidence"))
    assert result["stance"] == "NEUTRAL" and len(requests) == 1
    assert result.provenance["failure_code"] == code
    assert "SECRET BODY" not in str(result)


def test_timeout_keeps_the_unknown_spend_reserved_and_exhausted_budget_sends_nothing(tmp_path):
    async def timeout(request):
        raise httpx.ReadTimeout("SECRET ERROR", request=request)
    budget = SqliteAIBudget(tmp_path / "budget", daily_call_limit=1, daily_token_limit=100_000)
    model, requests = client(handler=timeout, budget=budget)
    first = asyncio.run(model.generate_trading_consensus("evidence"))
    second = asyncio.run(model.generate_trading_consensus("evidence"))
    assert first.provenance["failure_code"] == "provider_timeout"
    assert second.provenance["status"] == "budget_exhausted"
    assert len(requests) == 1 and budget.status("astra_consensus")["reserved_tokens"] > 0


def test_same_evidence_pair_preserves_primary_and_records_disagreement():
    primary = AnthropicSwarmClient(client=sdk(payload(stance="NEUTRAL")))
    secondary, requests = client(response(payload(stance="BUY")))
    wrapper = ChallengerConsensusClient(primary, secondary)
    result = asyncio.run(wrapper.generate_trading_consensus("identical evidence"))
    assert result["stance"] == "NEUTRAL"
    pair = result.provenance["model_comparison"]
    assert pair["primary"]["consensus"]["stance"] == "NEUTRAL"
    assert pair["challenger"]["consensus"]["stance"] == "BUY"
    assert pair["execution_authority"] == "primary_only"
    assert pair["outcome_status"] == "not_evaluated"
    assert pair["primary"]["prompt_sha256"] == pair["challenger"]["prompt_sha256"] == pair["prompt_sha256"]
    assert json.loads(requests[0].content)["input"] == primary._client.messages.create.await_args.kwargs["messages"][0]["content"]


def test_primary_failure_is_never_replaced_by_a_successful_astra_buy():
    primary = AnthropicSwarmClient(client=sdk(payload(confidence=2)))
    secondary, _ = client()
    atlas = AtlasInvestmentAgent(llm_client=ChallengerConsensusClient(primary, secondary))
    decision = asyncio.run(atlas.decide_with_llm("TRENT", MODERATE, NOW))
    assert decision.action.value == "NEUTRAL"
    assert decision.provenance["mode"] == "llm_invalid_schema"
    pair = decision.provenance["inference"]["model_comparison"]
    assert pair["primary"]["status"] == "invalid_schema"
    assert pair["challenger"]["status"] == "completed"


def test_failed_challenger_and_headline_forwarding_preserve_the_primary():
    primary = AnthropicSwarmClient(client=sdk(payload()))
    primary.score_headlines = AsyncMock(return_value={"scores": []})
    secondary, _ = client(status=500)
    secondary.generate_trading_consensus = AsyncMock(side_effect=RuntimeError("SECRET ERROR"))
    wrapper = ChallengerConsensusClient(primary, secondary)
    result = asyncio.run(wrapper.generate_trading_consensus("evidence"))
    assert result["stance"] == "BUY"
    assert result.provenance["model_comparison"]["challenger"]["failure_code"] == "provider_unavailable"
    assert "SECRET ERROR" not in str(result.provenance)
    assert asyncio.run(wrapper.score_headlines("TRENT", ("headline",))) == {"scores": []}
    primary.score_headlines.assert_awaited_once_with("TRENT", ("headline",))


def test_factory_off_by_default_and_durable_shadow_allowance_survives_restart(tmp_path, monkeypatch):
    monkeypatch.delenv("PRAMANA_ASTRA_SHADOW_ENABLED", raising=False)
    budget = SqliteAIBudget(tmp_path / "budget", daily_call_limit=100, daily_token_limit=1_000_000)
    primary = AnthropicSwarmClient(client=sdk(payload()), budget=budget)
    assert with_astra_challenger(primary, budget) is primary
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    monkeypatch.setenv("PRAMANA_ASTRA_SHADOW_ENABLED", "true")
    wrapped = with_astra_challenger(primary, budget)
    for _ in range(20):
        assert wrapped.challenger.budget.reserve("astra_consensus", 1)
    restarted = with_astra_challenger(primary, budget)
    assert not restarted.challenger.budget.reserve("astra_consensus", 1)
    assert budget.status("astra_consensus")["calls"] == 20


def test_missing_key_invalid_setting_and_unsupported_reasoning_fail_before_network(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        OpenAIConsensusClient()
    with pytest.raises(ValueError, match="reasoning"):
        OpenAIConsensusClient(api_key="test", reasoning_effort="none")
    monkeypatch.setenv("PRAMANA_ASTRA_SHADOW_ENABLED", "invalid")
    with pytest.raises(ValueError, match="true_or_false"):
        with_astra_challenger(None, None)


def test_pre_inference_holds_prevent_both_provider_calls():
    primary = AnthropicSwarmClient(client=sdk(payload()))
    secondary, requests = client()
    atlas = AtlasInvestmentAgent(llm_client=ChallengerConsensusClient(primary, secondary))
    held = asyncio.run(atlas.decide_with_llm("TRENT", MODERATE[:1], NOW))
    assert held.action.value == "NEUTRAL"
    primary._client.messages.create.assert_not_awaited()
    assert requests == []


def test_duplicate_json_fields_and_ambiguous_outputs_are_refused():
    raw = response()
    block = raw["output"][1]["content"][0]
    block["text"] = block["text"].replace('"confidence": 0.7', '"confidence": 0.1, "confidence": 0.7')
    model, _ = client(raw)
    with pytest.raises(ConsensusSchemaError):
        asyncio.run(model.generate_trading_consensus("evidence"))
    raw = response()
    raw["output"].append(raw["output"][1])
    model, _ = client(raw)
    with pytest.raises(ConsensusSchemaError):
        asyncio.run(model.generate_trading_consensus("evidence"))


def test_astra_smoke_probe_requires_opt_in_and_emits_only_metadata(tmp_path, monkeypatch, capsys):
    from quant_ai.llm import astra_probe
    assert astra_probe.main([]) == 2
    assert "paid_call_flag_required" in capsys.readouterr().out
    budget = SqliteAIBudget(tmp_path / "budget", daily_call_limit=1, daily_token_limit=100_000)
    budget.close()
    monkeypatch.setenv("PRAMANA_AI_BUDGET_DB", str(tmp_path / "budget"))
    monkeypatch.setenv("PRAMANA_AI_DAILY_CALL_LIMIT", "1")
    monkeypatch.setenv("PRAMANA_AI_DAILY_TOKEN_LIMIT", "100000")
    def factory(**kwargs):
        model, _ = client(response(payload(stance="NEUTRAL", confidence=0, expected_return=0,
                                          expected_risk=0, rationale=["SECRET MODEL TEXT"])), **kwargs)
        return model
    monkeypatch.setattr(astra_probe, "OpenAIConsensusClient", factory)
    assert astra_probe.main(["--confirm-paid-call"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["orders_enabled"] is False and report["passed"] is True
    assert "SECRET MODEL TEXT" not in str(report)
    assert astra_probe.main(["--confirm-paid-call"]) == 1  # shared budget blocks a repeat


def test_shadow_cannot_exceed_shared_account_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret")
    monkeypatch.setenv("PRAMANA_ASTRA_SHADOW_ENABLED", "true")
    budget = SqliteAIBudget(tmp_path / "budget", daily_call_limit=1, daily_token_limit=100_000)
    primary = AnthropicSwarmClient(client=sdk(payload()), budget=budget)
    wrapped = with_astra_challenger(primary, budget)
    wrapped.challenger._transport = httpx.MockTransport(lambda req: pytest.fail("must not call OpenAI"))
    result = asyncio.run(wrapped.generate_trading_consensus("evidence"))
    assert result["stance"] == "BUY"
    assert result.provenance["model_comparison"]["challenger"]["status"] == "budget_exhausted"
