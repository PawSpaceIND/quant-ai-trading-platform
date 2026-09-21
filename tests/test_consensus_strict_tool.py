"""The consensus tool is strict: the API guarantees the shape, the parser keeps the bounds.

On 21 September 2026 every consensus reply of the session was complete (stop_reason
tool_use, a few hundred output tokens) and still refused: 16 with fields missing, 7 with
fields added, 1 with a rationale that was not a string array. With ``strict: true`` the
API enforces the schema before the reply is returned, so those three failure modes cannot
recur; the numeric and length bounds the strict grammar cannot express remain enforced by
``parse_consensus``, and the field names of a refused payload are logged without values.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from quant_ai.llm import anthropic_client as adapter
from quant_ai.llm.anthropic_client import (
    STRICT_UNSUPPORTED_KEYWORDS,
    AnthropicSwarmClient,
    ConsensusSchemaError,
    _consensus_schema,
    _strict_input_schema,
)


def payload(**overrides):
    base = {
        "stance": "BUY", "confidence": 0.7, "expected_return": 0.02, "expected_risk": 0.01,
        "rationale": ["cited evidence"],
        "xai_proof": {"summary": "Paper-only view", "supporting_factors": ["a"], "risk_factors": ["b"]},
    }
    base.update(overrides)
    return base


def sdk(body, stop="tool_use", output=600):
    block = SimpleNamespace(type="tool_use", name="trading_consensus", input=body)
    usage = SimpleNamespace(input_tokens=4000, output_tokens=output,
                            cache_creation_input_tokens=0, cache_read_input_tokens=0)
    response = SimpleNamespace(model="synthetic-model", id="synthetic-id", stop_reason=stop,
                               content=[block], usage=usage)
    return SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=response)))


def keywords(node, found=None):
    found = set() if found is None else found
    if isinstance(node, dict):
        for key, value in node.items():
            found.add(key)
            keywords(value, found)
    elif isinstance(node, list):
        for item in node:
            keywords(item, found)
    return found


def test_request_carries_a_strict_tool_whose_schema_the_grammar_accepts():
    client = AnthropicSwarmClient(client=sdk(payload()), model="claude-sonnet-5")
    asyncio.run(client.generate_trading_consensus("TRENT market context"))
    (tool,) = client._client.messages.create.await_args.kwargs["tools"]
    assert tool["strict"] is True
    schema = tool["input_schema"]
    assert schema["required"] == ["stance", "confidence", "expected_return", "expected_risk", "rationale", "xai_proof"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["xai_proof"]["additionalProperties"] is False
    assert schema["properties"]["xai_proof"]["required"] == ["summary", "supporting_factors", "risk_factors"]
    assert set(schema["properties"]["stance"]["enum"]) >= {"BUY", "SELL", "NEUTRAL"}
    assert not keywords(schema) & STRICT_UNSUPPORTED_KEYWORDS


def test_the_client_side_contract_keeps_the_bounds_the_grammar_cannot_express():
    contract = _consensus_schema()
    assert contract["properties"]["confidence"]["minimum"] == 0
    assert contract["properties"]["confidence"]["maximum"] == 1
    assert contract["properties"]["rationale"]["minItems"] == 1
    assert contract["properties"]["xai_proof"]["properties"]["summary"]["minLength"] == 1
    strict = _strict_input_schema(contract)
    assert strict["properties"]["confidence"] == {"type": "number"}
    assert strict["properties"]["rationale"] == {"type": "array", "items": {"type": "string"}}
    assert strict["properties"]["xai_proof"]["properties"]["summary"] == {"type": "string"}
    # Nothing else moved: the strict schema is the contract minus those keywords.
    assert keywords(contract) - keywords(strict) <= STRICT_UNSUPPORTED_KEYWORDS
    assert _consensus_schema() == contract  # stripping never mutates the source


@pytest.mark.parametrize(
    "body, code",
    [
        ({k: v for k, v in payload().items() if k != "xai_proof"}, "missing_consensus_fields"),
        (payload(extra_field="PRIVATE_VALUE_NEVER_LOGGED"), "unknown_consensus_fields"),
        (payload(rationale="one string PRIVATE_VALUE_NEVER_LOGGED"), "rationale_not_string_array"),
        (payload(confidence=1.4), "confidence_out_of_range"),
        (payload(expected_risk=-0.2), "negative_expected_risk"),
    ],
)
def test_refused_payloads_keep_their_fixed_code_and_log_field_names_only(body, code, caplog):
    client = AnthropicSwarmClient(client=sdk(body), model="claude-sonnet-5")
    with caplog.at_level(logging.WARNING, logger=adapter.LOGGER.name), pytest.raises(ConsensusSchemaError) as refused:
        asyncio.run(client.generate_trading_consensus("TRENT market context"))
    assert refused.value.provenance["failure_code"] == code
    assert refused.value.provenance["status"] == "invalid_schema"
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("anthropic_consensus_invalid_schema"))
    assert f"code={code}" in line
    assert "keys=" in line
    assert "PRIVATE_VALUE_NEVER_LOGGED" not in caplog.text
    assert "PRIVATE_VALUE_NEVER_LOGGED" not in str(refused.value.provenance)


def test_logged_key_names_are_bounded_and_sorted():
    many = {f"field_{index:02d}": index for index in range(20)}
    names = adapter._payload_key_names(many)
    assert names.startswith("field_00,field_01") and names.endswith(",+4")
    assert adapter._payload_key_names({"b": 1, "a" * 80: 2}) == "a" * 40 + ",b"
    assert adapter._payload_key_names([1, 2]) == "list"
    assert adapter._payload_key_names({}) == "none"


def test_a_missing_tool_block_is_logged_with_the_block_types_seen(caplog):
    client = AnthropicSwarmClient(client=sdk(payload()), model="claude-sonnet-5")
    response = client._client.messages.create.return_value
    response.content = [SimpleNamespace(type="text", text="PRIVATE_TEXT_NEVER_LOGGED")]
    with caplog.at_level(logging.WARNING, logger=adapter.LOGGER.name), pytest.raises(ConsensusSchemaError) as refused:
        asyncio.run(client.generate_trading_consensus("TRENT market context"))
    assert refused.value.provenance["failure_code"] == "missing_consensus_tool"
    assert "code=missing_consensus_tool block_types=text" in caplog.text
    assert "PRIVATE_TEXT_NEVER_LOGGED" not in caplog.text


def test_a_conforming_reply_still_completes():
    client = AnthropicSwarmClient(client=sdk(payload()), model="claude-sonnet-5")
    result = asyncio.run(client.generate_trading_consensus("TRENT market context"))
    assert result.provenance["status"] == "completed"
    signal, proof = client.parse_consensus(result)
    assert signal.stance.value == "BUY" and proof.summary == "Paper-only view"


def test_an_empty_rationale_is_not_a_refusal_when_the_proof_carries_the_reasons(caplog):
    # The strict grammar cannot express minItems; on 21 September 2026 the model left
    # rationale empty in 10 of 36 complete replies and every one was refused for it.
    body = payload(rationale=[])
    client = AnthropicSwarmClient(client=sdk(body), model="claude-sonnet-5")
    with caplog.at_level(logging.INFO, logger=adapter.LOGGER.name):
        result = asyncio.run(client.generate_trading_consensus("TRENT market context"))
    assert result.provenance["status"] == "completed"
    assert result.provenance["rationale_source"] == "xai_summary"
    signal, proof = client.parse_consensus(result)
    assert signal.rationale == ("Paper-only view",)
    assert proof.summary == "Paper-only view"
    assert not [r for r in caplog.records if r.getMessage().startswith("anthropic_consensus_invalid_schema")]
    assert any(r.getMessage() == "anthropic_consensus_rationale_from_summary" for r in caplog.records)


def test_a_supplied_rationale_is_kept_and_recorded_as_the_source():
    client = AnthropicSwarmClient(client=sdk(payload()), model="claude-sonnet-5")
    result = asyncio.run(client.generate_trading_consensus("TRENT market context"))
    assert result.provenance["rationale_source"] == "rationale"
    signal, _ = client.parse_consensus(result)
    assert signal.rationale == ("cited evidence",)


def test_the_system_prompt_asks_for_a_non_empty_rationale():
    client = AnthropicSwarmClient(client=sdk(payload()), model="claude-sonnet-5")
    asyncio.run(client.generate_trading_consensus("TRENT market context"))
    system = client.client_request_system() if hasattr(client, "client_request_system") else client._client.messages.create.await_args.kwargs["system"]
    assert "at least one item" in system
    assert "never an empty list" in system


def test_a_timeout_is_logged_like_every_other_unavailability(caplog):
    async def never(**_):
        raise asyncio.TimeoutError
    transport = SimpleNamespace(messages=SimpleNamespace(create=never))
    client = AnthropicSwarmClient(client=transport, model="claude-sonnet-5", timeout_seconds=0.5)
    with caplog.at_level(logging.WARNING, logger=adapter.LOGGER.name):
        result = asyncio.run(client.generate_trading_consensus("TRENT market context"))
    assert result.provenance["status"] == "unavailable"
    assert result["rationale"] == ["Consensus Skipped: API Timeout"]
    assert "anthropic_consensus_unavailable detail=API Timeout timeout_seconds=0.5" in caplog.text
