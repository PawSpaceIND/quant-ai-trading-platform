import asyncio
import json

import pytest
from test_runtime_manifest import setup

from quant_ai.llm.anthropic_client import AnthropicSwarmClient
from quant_ai.llm.budget import SqliteAIBudget
from quant_ai.llm.challenger import with_astra_challenger


@pytest.fixture
def bound_pair(tmp_path, monkeypatch):
    monkeypatch.setenv("PRAMANA_ASTRA_SHADOW_ENABLED", "false")
    runner, source = setup(tmp_path, monkeypatch)
    budget = SqliteAIBudget(tmp_path / "private-budget.db", daily_call_limit=100,
                           daily_token_limit=1_000_000)
    primary = AnthropicSwarmClient(api_key="synthetic-primary-secret", budget=budget)
    monkeypatch.setenv("PRAMANA_ASTRA_SHADOW_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-challenger-secret")
    pair = with_astra_challenger(primary, budget)
    sample = pair.challenger.budget.sample
    runner.daemon.scheduler.pipeline.runtime.cio.atlas.llm_client = pair
    runner.daemon.bind_strategy_manifest(runner.streams, source_root=source, revision="a" * 40)
    try:
        yield runner, pair
    finally:
        asyncio.run(primary._client.close())
        sample.close()
        budget.close()
        runner.daemon.tracker.broker.close()


def test_challenger_configuration_is_supported_without_exporting_secrets(bound_pair, tmp_path):
    runner, pair = bound_pair
    monitor = runner.daemon.strategy_manifest
    first = monitor.check()
    assert first["status"] == "matched", first
    captured = monitor.capture()
    assert not captured["issues"]
    exported = json.dumps(captured)
    assert "primary_only" in exported
    assert "synthetic-primary-secret" not in exported
    assert "synthetic-challenger-secret" not in exported
    assert str(tmp_path) not in exported
    # Usage and credential rotation are operational state, not a new trading policy.
    pair.challenger._key = "synthetic-rotated-secret"
    pair.primary._client.api_key = "synthetic-rotated-primary"
    assert pair.challenger.budget.reserve("synthetic", 100)
    pair.challenger.budget.record("synthetic", {"input_tokens": 10, "output_tokens": 10},
                                 token_reservation=100)
    assert monitor.check()["sha256"] == first["sha256"]


@pytest.mark.parametrize("change", ["timeout", "tokens", "reasoning", "model", "shared_cap",
                                   "sample_cap", "budget_path", "disable", "endpoint"])
def test_challenger_policy_changes_invalidate_the_binding(bound_pair, monkeypatch, change):
    runner, pair = bound_pair
    monitor = runner.daemon.strategy_manifest
    if change == "timeout":
        pair.challenger.timeout_seconds = 20
    elif change == "tokens":
        pair.challenger.max_output_tokens = 4096
    elif change == "reasoning":
        pair.challenger.reasoning_effort = "high"
    elif change == "model":
        pair.challenger.model = "synthetic-different-model"
    elif change == "shared_cap":
        pair.primary.budget.daily_token_limit += 1
    elif change == "sample_cap":
        pair.challenger.budget.sample.daily_call_limit += 1
    elif change == "budget_path":
        pair.primary.budget.database += ".different"
    elif change == "endpoint":
        monkeypatch.setattr("quant_ai.llm.openai_client.RESPONSES_URL", "https://example.test/v1/responses")
    else:
        runner.daemon.scheduler.pipeline.runtime.cio.atlas.llm_client = pair.primary
    assert monitor.check()["status"] == "changed"


@pytest.mark.parametrize("change,issue", [
    ("transport", "unsupported_inference_transport"),
    ("budget", "challenger_shared_budget_mismatch"),
    ("shared", "challenger_shared_budget_mismatch"),
    ("sample", "unsupported_challenger_budget"),
    ("durability", "challenger_budget_not_durable"),
    ("primary", "unsupported_challenger_pair"),
])
def test_unverified_challenger_wiring_is_rejected(bound_pair, change, issue):
    runner, pair = bound_pair
    if change == "transport":
        pair.challenger._transport = object()
    elif change == "budget":
        pair.budget = None
    elif change == "shared":
        pair.challenger.budget.shared = pair.challenger.budget.sample
    elif change == "sample":
        pair.challenger.budget.sample = object()
    elif change == "durability":
        pair.challenger.budget.sample.database = ":memory:"
    else:
        pair.primary = object()
    assert issue in runner.daemon.strategy_manifest.capture()["issues"]
