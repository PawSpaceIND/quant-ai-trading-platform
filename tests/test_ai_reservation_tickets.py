"""Request-bound quota compensation never releases uncertain provider work."""
import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from quant_ai.llm.anthropic_client import AnthropicSwarmClient
from quant_ai.llm.budget import SqliteAIBudget
from quant_ai.llm.challenger import _ChallengerBudget
from quant_ai.llm.openai_client import OpenAIConsensusClient
from quant_ai.llm.spend import SpendRefused


def ledger(path, clock=None, calls=10):
    return SqliteAIBudget(path, daily_call_limit=calls, daily_token_limit=100000,
                          **({"clock": clock} if clock else {}))


def test_cancel_once_preserves_legacy_unknown_and_survives_restart(tmp_path):
    path = tmp_path / "tokens.sqlite"
    store = ledger(path)
    assert store.reserve("consensus", 500)  # Historical/unattributed admission.
    ticket = store.reserve_ticket("consensus", 700)
    store.close()
    store = ledger(path)
    assert store.cancel_ticket(ticket)
    assert not store.cancel_ticket(ticket)
    state = store.status("consensus")
    assert state["calls"] == 1 and state["reserved_tokens"] == 500
    assert state["aggregate"]["reserved_tokens"] == 500
    store.close()


def test_original_day_and_duplicate_settlement(tmp_path):
    now = [datetime(2026, 9, 30, 23, 59, tzinfo=timezone.utc)]
    path = tmp_path / "tokens.sqlite"
    store = ledger(path, lambda: now[0])
    ticket = store.reserve_ticket("consensus", 700)
    assert store.dispatch_ticket(ticket, {"id": "synthetic-dollar-ticket"})
    now[0] += timedelta(minutes=2)
    assert store.settle_ticket(ticket, {"input_tokens": 100, "output_tokens": 20})
    assert not store.settle_ticket(ticket, {"input_tokens": 100, "output_tokens": 20})
    assert store.status("consensus")["calls"] == 0
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT day,calls,input_tokens,output_tokens,reserved_tokens FROM ai_budget").fetchall() == [("2026-09-30", 1, 100, 20, 0)]
    store.close()


def test_dispatch_restart_missing_usage_cannot_cancel(tmp_path):
    path = tmp_path / "tokens.sqlite"
    store = ledger(path)
    ticket = store.reserve_ticket("consensus", 700)
    assert store.dispatch_ticket(ticket)
    store.close()
    store = ledger(path)
    assert not store.cancel_ticket(ticket)
    assert not store.settle_ticket(ticket, None)
    assert not store.dispatch_ticket(ticket)
    assert store.status("consensus")["reserved_tokens"] == 700
    store.close()


@pytest.mark.parametrize("mode", ["consensus", "headlines", "astra"])
def test_dollar_denial_compensates_and_never_dispatches(tmp_path, monkeypatch, mode):
    import quant_ai.llm.anthropic_client as anthropic
    import quant_ai.llm.openai_client as openai
    def denied(*args):
        raise SpendRefused()
    monkeypatch.setattr(anthropic, "require_reservation", denied)
    monkeypatch.setattr(openai, "require_reservation", denied)
    store = ledger(tmp_path / "tokens.sqlite")
    create = AsyncMock(side_effect=AssertionError("provider must not be called"))
    if mode == "astra":
        client = OpenAIConsensusClient(api_key="synthetic-key", budget=store)
        result = asyncio.run(client.generate_trading_consensus("synthetic prompt"))
        scope = "astra_consensus"
    else:
        client = AnthropicSwarmClient(client=SimpleNamespace(messages=SimpleNamespace(create=create)), budget=store)
        result = asyncio.run(client.score_headlines("NTPC", ("synthetic headline",))) if mode == "headlines" else asyncio.run(client.generate_trading_consensus("synthetic prompt"))
        scope = "headline_sentiment" if mode == "headlines" else "consensus"
    assert result.provenance["status"] == "budget_exhausted"
    create.assert_not_called()
    state = store.status(scope)
    assert state["calls"] == 0 and state["reserved_tokens"] == 0
    assert state["aggregate"]["calls"] == 0
    store.close()


def test_shadow_shared_denial_compensates_sample(tmp_path):
    shared = ledger(tmp_path / "tokens.sqlite", calls=1)
    assert shared.reserve("primary", 500)
    shadow = _ChallengerBudget(shared)
    assert shadow.reserve_ticket("astra_consensus", 700) is None
    assert shadow.sample.status("astra_consensus")["calls"] == 0
    assert shadow.sample.status("astra_consensus")["reserved_tokens"] == 0
    assert shared.status("primary")["reserved_tokens"] == 500
    shadow.sample.close()
    shared.close()


def test_request_tickets_share_atomic_caps_between_connections(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    path = tmp_path / "tokens.sqlite"
    first, second = ledger(path, calls=3), ledger(path, calls=3)
    with ThreadPoolExecutor(max_workers=2) as pool:
        tickets = list(pool.map(lambda i: (first if i % 2 else second).reserve_ticket("consensus", 700), range(20)))
    assert sum(ticket is not None for ticket in tickets) == 3
    assert first.status("consensus")["reserved_tokens"] == 2100
    first.close()
    second.close()


def test_cancel_after_midnight_uses_original_day(tmp_path):
    now = [datetime(2026, 9, 30, 23, 59, tzinfo=timezone.utc)]
    store = ledger(tmp_path / "tokens.sqlite", lambda: now[0])
    ticket = store.reserve_ticket("consensus", 700)
    now[0] += timedelta(minutes=2)
    assert store.cancel_ticket(ticket)
    with sqlite3.connect(store.database) as db:
        assert db.execute("SELECT calls,reserved_tokens FROM ai_budget_aggregate").fetchall() == [(0, 0)]
    store.close()


def test_inconsistent_counter_settlement_rolls_back_and_retains_ticket(tmp_path):
    store = ledger(tmp_path / "tokens.sqlite")
    ticket = store.reserve_ticket("consensus", 700)
    assert store.dispatch_ticket(ticket)
    # Simulated corruption must not partially subtract scope counters.
    with sqlite3.connect(store.database) as db:
        db.execute("UPDATE ai_budget_aggregate SET reserved_tokens=0")
    assert not store.settle_ticket(ticket, {"input_tokens": 10})
    assert store.status("consensus")["reserved_tokens"] == 700
    with sqlite3.connect(store.database) as db:
        assert db.execute("SELECT status FROM ai_budget_requests").fetchone() == ("dispatched",)
    store.close()


def test_partial_shadow_dispatch_cannot_release_marked_side(tmp_path, monkeypatch):
    shared = ledger(tmp_path / "tokens.sqlite")
    shadow = _ChallengerBudget(shared)
    ticket = shadow.reserve_ticket("astra_consensus", 700)
    monkeypatch.setattr(shared, "dispatch_ticket", lambda *args: False)
    assert not shadow.dispatch_ticket(ticket)
    shadow.cancel_ticket(ticket)
    assert shadow.sample.status("astra_consensus")["reserved_tokens"] == 700
    assert shared.status("astra_consensus")["reserved_tokens"] == 0
    shadow.sample.close()
    shared.close()
