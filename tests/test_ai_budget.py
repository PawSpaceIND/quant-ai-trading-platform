from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from quant_ai.agents.atlas import AtlasInvestmentAgent
from quant_ai.agents.contracts import AgentDomain, AgentEvidence, Stance
from quant_ai.llm.anthropic_client import AnthropicSwarmClient
from quant_ai.llm.budget import (
    DEFAULT_DAILY_CALL_LIMIT,
    DEFAULT_DAILY_TOKEN_LIMIT,
    SqliteAIBudget,
    budget_from_env,
)

NOON = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)


class _Clock:
    def __init__(self, now: datetime = NOON) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _budget(tmp_path, *, calls: int = 3, tokens: int = 1_000, clock: _Clock | None = None):
    return SqliteAIBudget(
        tmp_path / "ai-budget.sqlite",
        daily_call_limit=calls,
        daily_token_limit=tokens,
        clock=clock or _Clock(),
    )


def _payload() -> dict[str, object]:
    return {
        "stance": "BUY",
        "confidence": 0.82,
        "expected_return": 0.03,
        "expected_risk": 0.02,
        "rationale": ["fresh_tick_supports_upside"],
        "xai_proof": {
            "summary": "Paper-only consensus.",
            "supporting_factors": ["fresh_tick"],
            "risk_factors": ["model_uncertainty"],
        },
    }


def _sdk(input_tokens: int = 120, output_tokens: int = 40) -> SimpleNamespace:
    response = SimpleNamespace(
        model="claude-resolved", id="msg-1",
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
        content=[SimpleNamespace(type="tool_use", name="trading_consensus", input=_payload())],
    )
    return SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=response)))


def _sealed_sdk() -> SimpleNamespace:
    """An SDK that must never be reached: any call is a test failure."""
    create = AsyncMock(side_effect=AssertionError("Anthropic SDK was called over budget"))
    return SimpleNamespace(messages=SimpleNamespace(create=create))


def _evidence(now: datetime):
    return tuple(
        AgentEvidence(f"synthetic-{i}", AgentDomain.TECHNICAL, "INFY", Stance.BUY,
                      D(".8"), D(".03"), D(".02"), ("synthetic",), now, 0)
        for i in range(4)
    )


def test_reserve_counts_calls_and_refuses_at_the_call_limit(tmp_path) -> None:
    budget = _budget(tmp_path, calls=3)
    assert [budget.reserve("consensus") for _ in range(3)] == [True, True, True]
    assert budget.reserve("consensus") is False
    assert budget.reserve("consensus") is False
    status = budget.status("consensus")
    assert status["day"] == "2026-09-15"
    assert status["calls"] == 3 and status["remaining_calls"] == 0 and status["exhausted"]
    assert status["daily_call_limit"] == 3 and status["daily_token_limit"] == 1_000
    # Scopes are independent counters.
    assert budget.reserve("other") is True
    assert budget.status("other")["calls"] == 1


def test_token_limit_refuses_further_calls_and_ignores_malformed_usage(tmp_path) -> None:
    budget = _budget(tmp_path, calls=10, tokens=100)
    assert budget.reserve("consensus") is True
    budget.record("consensus", None)
    budget.record("consensus", {"input_tokens": None, "output_tokens": "12"})
    budget.record("consensus", {"input_tokens": -5, "output_tokens": True})
    assert budget.status("consensus")["tokens"] == 0
    budget.record("consensus", {"input_tokens": 60, "output_tokens": 39})
    assert budget.reserve("consensus") is True
    budget.record("consensus", {"input_tokens": 1})
    status = budget.status("consensus")
    assert status["input_tokens"] == 61 and status["output_tokens"] == 39
    assert status["remaining_tokens"] == 0 and status["remaining_calls"] == 8
    assert budget.reserve("consensus") is False


def test_counters_reset_on_the_utc_day_rollover(tmp_path) -> None:
    clock = _Clock(datetime(2026, 9, 15, 23, 59, tzinfo=timezone.utc))
    budget = _budget(tmp_path, calls=1, clock=clock)
    assert budget.reserve("consensus") is True
    budget.record("consensus", {"input_tokens": 10, "output_tokens": 5})
    assert budget.reserve("consensus") is False
    clock.now += timedelta(minutes=1)
    assert budget.current_day() == "2026-09-16"
    assert budget.reserve("consensus") is True
    assert budget.status("consensus")["tokens"] == 0
    # Yesterday's row is retained for the record, not rolled forward.
    rows = sqlite3.connect(str(tmp_path / "ai-budget.sqlite")).execute(
        "SELECT day, calls, input_tokens FROM ai_budget ORDER BY day"
    ).fetchall()
    assert rows == [("2026-09-15", 1, 10), ("2026-09-16", 1, 0)]


def test_concurrent_reserve_never_over_admits(tmp_path) -> None:
    limit = 40
    # Two handles on one file stand in for the cadence and protection threads and for
    # a second process sharing the ledger directory.
    handles = [_budget(tmp_path, calls=limit) for _ in range(2)]
    admitted: list[bool] = []
    guard = threading.Lock()
    barrier = threading.Barrier(8)

    def worker(index: int) -> None:
        budget = handles[index % 2]
        barrier.wait()
        results = [budget.reserve("consensus") for _ in range(25)]
        with guard:
            admitted.extend(results)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(admitted) == 200
    assert sum(admitted) == limit
    assert handles[0].status("consensus")["calls"] == limit


def test_budget_database_errors_fail_closed(tmp_path, caplog) -> None:
    budget = _budget(tmp_path, calls=5)
    assert budget.reserve("consensus") is True
    budget.close()
    with caplog.at_level(logging.ERROR, logger="quant_ai.ai_budget"):
        assert budget.reserve("consensus") is False
        budget.record("consensus", {"input_tokens": 1, "output_tokens": 1})
    assert "ai_budget_unavailable" in caplog.text
    assert "ai_budget_record_failed" in caplog.text


def test_client_returns_neutral_without_calling_the_sdk_when_exhausted(tmp_path, caplog) -> None:
    clock = _Clock()
    budget = _budget(tmp_path, calls=1, clock=clock)
    assert budget.reserve("consensus") is True
    sdk = _sealed_sdk()
    client = AnthropicSwarmClient(client=sdk, budget=budget)

    with caplog.at_level(logging.WARNING, logger="quant_ai.anthropic"):
        first = asyncio.run(client.generate_trading_consensus("INFY market context"))
        second = asyncio.run(client.generate_trading_consensus("INFY market context"))
        clock.now += timedelta(days=1)
        budget.reserve("consensus")  # the new day's single call is spent elsewhere
        third = asyncio.run(client.generate_trading_consensus("INFY market context"))

    sdk.messages.create.assert_not_awaited()
    for result in (first, second, third):
        signal, proof = client.parse_consensus(result)
        assert signal.stance is Stance.NEUTRAL and signal.confidence == 0
        assert result["rationale"] == ["Consensus Skipped: AI budget exhausted"]
        assert proof.risk_factors == ("ai_budget_exhausted",)
        assert result.provenance["status"] == "budget_exhausted"
        assert result.provenance["failure"] == "AI budget exhausted"
        assert result.provenance["resolved_model"] is None
        assert result.provenance["prompt_sha256"]
    warnings = [r for r in caplog.records if "anthropic_consensus_budget_exhausted" in r.message]
    assert [record.levelno for record in warnings] == [logging.WARNING, logging.WARNING]
    assert "day=2026-09-15" in warnings[0].message and "day=2026-09-16" in warnings[1].message


def test_exhausted_budget_degrades_the_atlas_decision_to_neutral(tmp_path) -> None:
    budget = _budget(tmp_path, calls=1)
    assert budget.reserve("consensus") is True
    client = AnthropicSwarmClient(client=_sealed_sdk(), budget=budget)
    now = datetime.now(timezone.utc)

    decision = asyncio.run(
        AtlasInvestmentAgent(llm_client=client).decide_with_llm("INFY", _evidence(now), now)
    )

    assert decision.action is Stance.NEUTRAL
    assert decision.provenance["inference"]["status"] == "budget_exhausted"
    assert "xai_summary=Consensus Skipped: AI budget exhausted" in decision.rationale


def test_usage_is_recorded_after_a_completed_call(tmp_path) -> None:
    budget = _budget(tmp_path, calls=5, tokens=200)
    sdk = _sdk(input_tokens=120, output_tokens=40)
    client = AnthropicSwarmClient(client=sdk, budget=budget)

    first = asyncio.run(client.generate_trading_consensus("INFY market context"))
    assert first.provenance["status"] == "completed"
    assert first.provenance["usage"]["input_tokens"] == 120
    status = budget.status("consensus")
    assert status["calls"] == 1 and status["input_tokens"] == 120 and status["output_tokens"] == 40

    second = asyncio.run(client.generate_trading_consensus("INFY market context"))
    assert second.provenance["status"] == "completed"
    assert budget.status("consensus")["tokens"] == 320
    # 320 tokens exceed the 200-token cap, so the third call never reaches the SDK.
    third = asyncio.run(client.generate_trading_consensus("INFY market context"))
    assert third.provenance["status"] == "budget_exhausted"
    assert sdk.messages.create.await_count == 2
    assert budget.status("consensus")["calls"] == 2


def test_client_without_budget_is_unbounded(tmp_path) -> None:
    sdk = _sdk()
    client = AnthropicSwarmClient(client=sdk)
    for _ in range(3):
        assert asyncio.run(client.generate_trading_consensus("x")).provenance["status"] == "completed"
    assert sdk.messages.create.await_count == 3


def test_budget_from_env_defaults_overrides_and_disable(tmp_path) -> None:
    budget = budget_from_env(tmp_path / "ledger", environ={})
    assert isinstance(budget, SqliteAIBudget)
    assert budget.daily_call_limit == DEFAULT_DAILY_CALL_LIMIT == 500
    assert budget.daily_token_limit == DEFAULT_DAILY_TOKEN_LIMIT == 2_000_000
    assert budget.database == str(tmp_path / "ledger" / "ai-budget.sqlite")
    assert (tmp_path / "ledger" / "ai-budget.sqlite").exists()
    budget.close()

    custom = budget_from_env(tmp_path, environ={
        "PRAMANA_AI_DAILY_CALL_LIMIT": " 12 ",
        "PRAMANA_AI_DAILY_TOKEN_LIMIT": "3456",
        "PRAMANA_AI_BUDGET_DB": str(tmp_path / "elsewhere" / "spend.sqlite"),
    })
    assert isinstance(custom, SqliteAIBudget)
    assert (custom.daily_call_limit, custom.daily_token_limit) == (12, 3456)
    assert custom.database == str(tmp_path / "elsewhere" / "spend.sqlite")
    custom.close()

    for variable, value in (
        ("PRAMANA_AI_DAILY_CALL_LIMIT", "0"),
        ("PRAMANA_AI_DAILY_CALL_LIMIT", "-1"),
        ("PRAMANA_AI_DAILY_TOKEN_LIMIT", "0"),
    ):
        assert budget_from_env(tmp_path, environ={variable: value}) is None

    with pytest.raises(ValueError, match="PRAMANA_AI_DAILY_CALL_LIMIT"):
        budget_from_env(tmp_path, environ={"PRAMANA_AI_DAILY_CALL_LIMIT": "many"})
    with pytest.raises(ValueError):
        SqliteAIBudget(tmp_path / "x.sqlite", daily_call_limit=0, daily_token_limit=1)
