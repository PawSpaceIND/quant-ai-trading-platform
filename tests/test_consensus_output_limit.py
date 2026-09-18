"""Offline output-limit and completion-boundary regressions; no account calls."""
from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import yaml

from quant_ai.governance.runtime_manifest import describe, digest
from quant_ai.llm import anthropic_client as adapter
from quant_ai.llm.budget import SqliteAIBudget
from quant_ai.llm.provenance import content_hash

ENV = "PRAMANA_CONSENSUS_MAX_TOKENS"


@pytest.fixture(autouse=True)
def isolated_setting(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)


def body():
    return {"stance": "NEUTRAL", "confidence": 0.4, "expected_return": 0.0,
            "expected_risk": 0.01, "rationale": ["Synthetic evidence is inconclusive."],
            "xai_proof": {"summary": "No paper entry supported.",
                          "supporting_factors": [], "risk_factors": ["uncertainty"]}}


def response(stop="tool_use", output=1800, payload=None):
    return SimpleNamespace(model="synthetic-model", id="synthetic-response", stop_reason=stop,
        usage=SimpleNamespace(input_tokens=80, output_tokens=output),
        content=[SimpleNamespace(type="tool_use", name="trading_consensus",
                                 input=body() if payload is None else payload)])


def make(*, cap=None, stop="tool_use", output=1800, budget=None):
    create = AsyncMock(return_value=response(stop, output))
    sdk = SimpleNamespace(messages=SimpleNamespace(create=create))
    kwargs = {} if cap is None else {"consensus_max_tokens": cap}
    return adapter.AnthropicSwarmClient(client=sdk, budget=budget, **kwargs), create


def run(client):
    return asyncio.run(client.generate_trading_consensus("Synthetic supplied market context"))


def test_default_retains_legacy_cap_and_protocol():
    client, create = make(output=700)
    assert client.consensus_max_tokens == 1200
    assert run(client).provenance["status"] == "completed"
    request = create.await_args.kwargs
    assert request["max_tokens"] == 1200
    assert "Keep the response compact" not in request["system"]
    assert "thinking" not in request


@pytest.mark.parametrize("cap", [1200, 1201, 2400, 4096])
def test_explicit_valid_output_limits_reach_request(cap):
    client, create = make(cap=cap, output=700)
    result = run(client)
    assert create.await_args.kwargs["max_tokens"] == cap
    assert result.provenance["request"]["max_tokens"] == cap
    assert result.provenance["request_sha256"] == content_hash(create.await_args.kwargs)


def test_environment_selects_bounded_profile_and_preserves_schema(monkeypatch):
    legacy, old_call = make(output=700)
    run(legacy)
    monkeypatch.setenv(ENV, "4096")
    revised, call = make()
    reply = run(revised)
    request = call.await_args.kwargs
    assert revised.consensus_max_tokens == request["max_tokens"] == 4096
    assert "Keep the response compact" in request["system"]
    assert "at most three short items" in request["system"]
    assert request["tools"] == old_call.await_args.kwargs["tools"]
    assert request["tool_choice"] == old_call.await_args.kwargs["tool_choice"]
    assert request["model"] == old_call.await_args.kwargs["model"]
    assert "thinking" not in request
    assert reply.provenance["request_sha256"] != content_hash(old_call.await_args.kwargs)
    assert reply.provenance["status"] == "completed"
    assert call.await_count == 1


def test_explicit_constructor_value_overrides_environment(monkeypatch):
    monkeypatch.setenv(ENV, "2400")
    client, create = make(cap=4096)
    run(client)
    assert create.await_args.kwargs["max_tokens"] == 4096


@pytest.mark.parametrize("value", ["0", "1199", "4097", "+2400", "2_400", "2400.0",
    "2.4e3", "true", "NaN", "-1", "02400", "９９９９", "9" * 200, "PRIVATE_BAD_VALUE"])
def test_invalid_environment_refuses_before_sdk_creation(monkeypatch, value):
    monkeypatch.setenv(ENV, value)
    factory = Mock()
    monkeypatch.setattr(adapter, "AsyncAnthropic", factory)
    with pytest.raises(ValueError, match="^consensus_max_tokens_invalid$") as caught:
        adapter.AnthropicSwarmClient(api_key="synthetic-key")
    assert str(caught.value) == "consensus_max_tokens_invalid"
    factory.assert_not_called()


@pytest.mark.parametrize("value", [True, False, 1200.0, "2400", -1, 1199, 4097])
def test_invalid_constructor_types_and_range_refuse(value):
    with pytest.raises(ValueError, match="^consensus_max_tokens_invalid$"):
        make(cap=value)


def test_five_truncated_controls_and_complete_bounded_replies():
    # Protocol emulation only: no claim that a live model necessarily fits 4096.
    async def emulate(**request):
        return response("max_tokens", 1200, {}) if request["max_tokens"] == 1200 else response()
    for symbol in ("INFY", "TCS", "RELIANCE", "GOLDBEES", "SILVERBEES"):
        old, old_call = make()
        old_call.side_effect = emulate
        with pytest.raises(adapter.ConsensusSchemaError) as caught:
            asyncio.run(old.generate_trading_consensus("synthetic " + symbol))
        assert caught.value.provenance["failure_code"] == "output_truncated"
        fixed, new_call = make(cap=4096)
        new_call.side_effect = emulate
        reply = asyncio.run(fixed.generate_trading_consensus("synthetic " + symbol))
        assert reply.provenance["status"] == "completed"
        assert reply["stance"] == "NEUTRAL"
        assert old_call.await_count == new_call.await_count == 1


def test_larger_cap_does_not_accept_truncated_valid_looking_reply():
    client, create = make(cap=4096, stop="max_tokens", output=4096)
    with pytest.raises(adapter.ConsensusSchemaError) as caught:
        run(client)
    assert caught.value.provenance["failure_code"] == "output_truncated"
    assert caught.value.provenance["usage"]["output_tokens"] == 4096
    assert create.await_count == 1


@pytest.mark.parametrize("stop", ["end_turn", "stop_sequence", "PRIVATE_UNKNOWN_STOP", None])
def test_real_transport_requires_completed_tool_stop(stop):
    client, create = make(stop=stop, output=700)
    # SDK-like fixture; no live client constructed or request performed.
    client.transport_kind = "anthropic_sdk"
    with pytest.raises(adapter.ConsensusSchemaError) as caught:
        run(client)
    assert caught.value.provenance["failure_code"] == "completion_unverified"
    assert "PRIVATE_UNKNOWN_STOP" not in json.dumps(caught.value.provenance)
    assert create.await_count == 1


def test_legacy_injected_missing_stop_remains_explicitly_unknown():
    client, _ = make(stop=None, output=700)
    reply = run(client)
    assert reply.provenance["stop_reason"] is None
    assert reply.provenance["transport"] == "injected_client"


def test_response_validation_exception_does_not_echo_unknown_fields():
    client, create = make(output=700)
    malformed = body()
    malformed["PRIVATE_FIELD_NAME"] = "PRIVATE_VALUE"
    create.return_value = response(payload=malformed, output=700)
    with pytest.raises(adapter.ConsensusSchemaError) as caught:
        run(client)
    assert caught.value.provenance["failure_code"] == "unknown_consensus_fields"
    assert "PRIVATE_" not in str(caught.value)
    assert "PRIVATE_" not in json.dumps(caught.value.provenance)


def test_configuration_identity_includes_actual_output_allowance():
    first, _ = make(cap=1200)
    second, _ = make(cap=4096)
    a, b = describe(first, []), describe(second, [])
    assert a["parameters"].get("consensus_max_tokens") == 1200
    assert b["parameters"].get("consensus_max_tokens") == 4096
    assert digest(a) != digest(b)


def test_compose_exposes_opt_in_limit_without_raising_daily_budgets():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    compose = yaml.safe_load((root / "deploy/docker-compose.yml").read_text())
    env = compose["services"]["pramana-ghost"]["environment"]
    assert env.get(ENV) == "${PRAMANA_CONSENSUS_MAX_TOKENS:-1200}"
    assert env["PRAMANA_AI_DAILY_CALL_LIMIT"] == "${PRAMANA_AI_DAILY_CALL_LIMIT:-500}"
    assert env["PRAMANA_AI_DAILY_TOKEN_LIMIT"] == "${PRAMANA_AI_DAILY_TOKEN_LIMIT:-2000000}"
    assert env["TRADING_LIVE_MONEY_ACTIVE"] == "false"
    for name, service in compose["services"].items():
        if name != "pramana-ghost":
            assert ENV not in service.get("environment", {})


def test_spend_is_recorded_before_truncated_reply_and_no_budget_bypass(tmp_path):
    budget = SqliteAIBudget(tmp_path / "budget.sqlite", daily_call_limit=1, daily_token_limit=5000)
    try:
        client, create = make(cap=4096, stop="max_tokens", output=4096, budget=budget)
        with pytest.raises(adapter.ConsensusSchemaError):
            run(client)
        assert budget.status("consensus")["tokens"] == 4176
        assert run(client).provenance["status"] == "budget_exhausted"
        assert create.await_count == 1
    finally:
        budget.close()


def test_original_numeric_schema_remains_strict_with_larger_cap():
    for field, value in (("confidence", "0.8"), ("confidence", 1.1),
                         ("expected_risk", -0.01), ("expected_return", float("nan"))):
        client, create = make(cap=4096, output=700)
        invalid = copy.deepcopy(body())
        invalid[field] = value
        create.return_value = response(payload=invalid, output=700)
        with pytest.raises(adapter.ConsensusSchemaError):
            run(client)
        assert create.await_count == 1


def test_exhausted_daily_budget_prevents_all_sdk_requests(tmp_path):
    budget = SqliteAIBudget(tmp_path / "spent.sqlite", daily_call_limit=1, daily_token_limit=5000)
    try:
        assert budget.reserve("consensus")
        client, create = make(cap=4096, budget=budget)
        reply = run(client)
        assert reply.provenance["status"] == "budget_exhausted"
        assert create.await_count == 0
    finally:
        budget.close()


def test_metadata_projection_is_bounded_and_omits_provider_text():
    source = response(stop="PRIVATE_STOP", output=700)
    source.content = [SimpleNamespace(type="PRIVATE_TYPE", text="PRIVATE_TEXT")] + [
        SimpleNamespace(type="text", text="PRIVATE_TEXT") for _ in range(35)] + source.content
    meta = adapter._consensus_response_metadata(source)
    assert meta.get("stop_reason") == "unrecognized"
    assert len(meta.get("content_block_types", [])) == 32
    assert meta.get("content_block_count") == 37
    assert meta.get("matching_tool_blocks") == 1
    assert "PRIVATE" not in json.dumps(meta)


def test_nonobject_tool_refused_before_payload_parser(monkeypatch):
    client, create = make(output=700)
    create.return_value = response(payload=["not-an-object"], output=700)
    parser = Mock(return_value=None)
    monkeypatch.setattr(client, "parse_consensus", parser)
    with pytest.raises(adapter.ConsensusSchemaError) as caught:
        run(client)
    assert caught.value.provenance["failure_code"] == "tool_input_not_object"
    parser.assert_not_called()
