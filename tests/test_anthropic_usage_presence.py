"""Exercise provider wire usage through the real SDK, with HTTP entirely mocked."""
import asyncio
import sqlite3

import anthropic
import anthropic._base_client as sdk_base
import pytest
from test_consensus_strict_tool import payload

from quant_ai.llm import anthropic_client as adapter
from quant_ai.llm.budget import SqliteAIBudget


@pytest.mark.parametrize("mode", ["consensus", "headlines"])
@pytest.mark.parametrize("cache,settled", [
    ({}, True),
    ({"cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}, True),
    ({"cache_creation_input_tokens": 30, "cache_read_input_tokens": 40}, True),
    ({"cache_creation_input_tokens": None}, False),
    ({"cache_read_input_tokens": None}, False),
    ({"cache_read_input_tokens": -1}, False),
    ({"cache_read_input_tokens": "unknown"}, False),
    ({"cache_read_input_tokens": True}, False),
])
def test_real_sdk_absent_cache_defaults_differ_from_explicit_unknown(tmp_path, monkeypatch, mode, cache, settled):
    monkeypatch.setattr(adapter, "require_reservation", lambda request: None)
    http = getattr(sdk_base, "httpx2", None) or sdk_base.httpx
    requests = []
    usage = {"input_tokens": 10, "output_tokens": 20, **cache}
    tool_input = payload() if mode == "consensus" else {
        "scores": [{"index": 0, "sentiment": 0.5, "rationale": "Synthetic evidence"}]}
    name = "trading_consensus" if mode == "consensus" else adapter.HEADLINE_TOOL_NAME

    async def handler(request):
        requests.append(request)
        return http.Response(200, json={
            "id": "synthetic-response", "type": "message", "role": "assistant",
            "model": "synthetic-model", "stop_reason": "tool_use", "stop_sequence": None,
            "content": [{"id": "synthetic-tool", "type": "tool_use", "name": name, "input": tool_input}],
            "usage": usage,
        })

    store = SqliteAIBudget(tmp_path / "tokens", daily_call_limit=10, daily_token_limit=100000)

    async def run():
        async with http.AsyncClient(transport=http.MockTransport(handler)) as http_client:
            sdk = anthropic.AsyncAnthropic(api_key="synthetic-key", http_client=http_client, max_retries=0)
            client = adapter.AnthropicSwarmClient(client=sdk, budget=store)
            return await client.generate_trading_consensus("synthetic prompt") if mode == "consensus" else await client.score_headlines("NTPC", ("Synthetic headline",))

    result = asyncio.run(run())
    assert result.provenance["status"] == "completed"
    scope = "consensus" if mode == "consensus" else "headline_sentiment"
    state = store.status(scope)
    assert len(requests) == 1
    for optional in ("cache_creation_input_tokens", "cache_read_input_tokens"):
        assert (optional in result.provenance["usage"]) == (optional in cache)
    with sqlite3.connect(store.database) as db:
        status, allowance = db.execute("SELECT status,reserved_tokens FROM ai_budget_requests").fetchone()
    assert status == ("settled" if settled else "dispatched")
    assert state["reserved_tokens"] == (0 if settled else allowance)
    if settled:
        assert state["input_tokens"] == 10 + sum(cache.values())
        assert state["output_tokens"] == 20
    store.close()
