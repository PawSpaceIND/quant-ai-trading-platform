from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from quant_ai.llm.spend import SpendRefused, estimate, initialize, reserve, settle, usage_micro

PAYLOAD = {"model": "claude-sonnet-5", "max_tokens": 1400, "system": "test",
           "messages": [{"role": "user", "content": "test"}]}
DAY = "2026-09-22"


def environment(tmp_path):
    return {"PRAMANA_AI_DAILY_USD_LIMIT": "2.50", "PRAMANA_AI_SPEND_DB": str(tmp_path / "spend.db")}


def open_day(env):
    assert reserve(PAYLOAD, env, day="2026-09-21") is False


def counters(env, day=DAY):
    with sqlite3.connect(env["PRAMANA_AI_SPEND_DB"]) as db:
        return db.execute("SELECT spent_micro,reserved_micro,carried_micro,limit_micro FROM ai_spend_days WHERE day=?", (day,)).fetchone()


def test_activation_does_not_reset_unknown_historical_spend_and_next_day_opens(tmp_path):
    env = environment(tmp_path)
    open_day(env)
    assert counters(env, "2026-09-21") == (0, 0, 2_500_000, 2_500_000)
    ticket = reserve(PAYLOAD, env, day=DAY)
    assert ticket
    assert counters(env)[1] == estimate(PAYLOAD, DAY)


def test_boot_initializes_hold_even_without_a_paid_call(tmp_path):
    env = environment(tmp_path)
    assert initialize(env, day="2026-09-21")
    with sqlite3.connect(env["PRAMANA_AI_SPEND_DB"]) as db:
        assert db.execute("SELECT COUNT(*) FROM ai_spend_requests").fetchone()[0] == 0
    assert initialize(env, day=DAY)
    assert reserve(PAYLOAD, env, day=DAY)


def test_sdk_does_not_hide_unreserved_retries():
    from quant_ai.llm.anthropic_client import AnthropicSwarmClient
    client = AnthropicSwarmClient(api_key="synthetic")
    assert client._client.max_retries == 0


def test_restart_concurrency_and_both_providers_share_one_cap(tmp_path):
    env = environment(tmp_path)
    open_day(env)
    astra = {"model": "gpt-6-astra", "max_output_tokens": 8192, "input": "same context"}
    def send(i):
        return reserve(PAYLOAD if i % 2 else astra, env, day=DAY)
    with ThreadPoolExecutor(max_workers=8) as pool:
        tickets = list(pool.map(send, range(160)))
    assert any(t is False for t in tickets) and any(t for t in tickets)
    spent, reserved, carried, cap = counters(env)
    assert 0 < reserved <= cap == 2_500_000 and spent == carried == 0
    with sqlite3.connect(env["PRAMANA_AI_SPEND_DB"]) as db:
        assert db.execute("SELECT SUM(reserved_micro) FROM ai_spend_requests").fetchone()[0] == reserved
    assert reserve(astra, env, day=DAY) is False


def test_settlement_idempotent_and_charged_to_request_day(tmp_path):
    env = environment(tmp_path)
    open_day(env)
    ticket = reserve(PAYLOAD, env, day=DAY)
    usage = {"input_tokens": 1000, "output_tokens": 100, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 100}
    settle(ticket, usage)
    settle(ticket, usage)
    assert counters(env) == (3420, 0, 0, 2_500_000)
    # A later-day reservation cannot move an earlier completion to that new day.
    assert reserve(PAYLOAD, env, day="2026-09-23")
    settle(ticket, usage)
    assert counters(env)[0] == 3420
    assert counters(env, "2026-09-23")[0] == 0


@pytest.mark.parametrize("usage", [None, {}, {"input_tokens": 100}, {"input_tokens": True, "output_tokens": 4}, {"input_tokens": -1, "output_tokens": 4}])
def test_unknown_or_invalid_usage_retains_full_reservation(tmp_path, usage):
    env = environment(tmp_path)
    open_day(env)
    ticket = reserve(PAYLOAD, env, day=DAY)
    before = counters(env)
    settle(ticket, usage)
    assert counters(env) == before


@pytest.mark.parametrize("change", [{"model": "unknown"}, {"max_tokens": True}, {"max_tokens": 20000}, {"service_tier": "priority"}, {"tools": [{"type": "web_search"}]}, {"messages": [{"content": "X" * 125000}]}])
def test_unpriced_requests_fail_before_reserving(tmp_path, change):
    with pytest.raises(SpendRefused):
        reserve({**PAYLOAD, **change}, environment(tmp_path), day=DAY)


def test_expired_prices_and_broken_database_fail_closed(tmp_path):
    env = environment(tmp_path)
    with pytest.raises(SpendRefused):
        reserve(PAYLOAD, env, day="2026-10-22")
    (tmp_path / "spend.db").write_text("corrupt")
    with pytest.raises(SpendRefused):
        reserve(PAYLOAD, env, day=DAY)


def test_lowering_cap_does_not_reset_usage_or_raise_it_from_other_process(tmp_path):
    env = environment(tmp_path)
    open_day(env)
    assert reserve(PAYLOAD, env, day=DAY)
    assert reserve(PAYLOAD, {**env, "PRAMANA_AI_DAILY_USD_LIMIT": "0.01"}, day=DAY) is False
    assert reserve(PAYLOAD, env, day=DAY) is False
    assert counters(env)[3] == 10000


def test_astra_reasoning_cost_in_output_cached_input_not_double_counted():
    assert usage_micro("gpt-6-astra", {"input_tokens": 1000, "output_tokens": 100,
           "input_tokens_details": {"cached_tokens": 500}}) == 17500


def test_real_adapters_refuse_paid_io_when_usd_allowance_is_closed(tmp_path, monkeypatch):
    from test_astra_challenger import client
    from test_consensus_strict_tool import payload, sdk

    from quant_ai.llm.anthropic_client import AnthropicSwarmClient

    env = environment(tmp_path)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    primary = AnthropicSwarmClient(client=sdk(payload()))
    result = asyncio.run(primary.generate_trading_consensus("evidence"))
    assert result.provenance["status"] == "budget_exhausted"
    primary._client.messages.create.assert_not_awaited()
    astra, calls = client()
    result = asyncio.run(astra.generate_trading_consensus("evidence"))
    assert result.provenance["status"] == "budget_exhausted" and calls == []


def test_actual_adapter_request_shapes_can_settle_against_shared_limit(tmp_path, monkeypatch):
    from test_astra_challenger import client
    from test_consensus_strict_tool import payload, sdk

    from quant_ai.llm.anthropic_client import AnthropicSwarmClient

    env = environment(tmp_path)
    open_day(env)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr("quant_ai.llm.spend.utc_day", lambda: DAY)
    primary = AnthropicSwarmClient(model="claude-sonnet-5", client=sdk(payload()))
    asyncio.run(primary.generate_trading_consensus("synthetic evidence"))
    primary._client.messages.create.assert_awaited_once()
    astra, calls = client()
    asyncio.run(astra.generate_trading_consensus("synthetic evidence"))
    assert len(calls) == 1
    # Sonnet 4000 input/600 output + Astra 100 input/200 output.
    assert counters(env) == (25250, 0, 0, 2500000)


def test_corrupt_activation_fails_closed_without_crashing_startup(tmp_path):
    env = environment(tmp_path)
    open_day(env)
    with sqlite3.connect(env["PRAMANA_AI_SPEND_DB"]) as db:
        db.execute("UPDATE ai_spend_policy SET activation_day='invalid'")
    assert initialize(env, day=DAY) is False
    with pytest.raises(SpendRefused):
        reserve(PAYLOAD, env, day=DAY)


def test_closed_paid_budget_does_not_stop_independent_exit_and_reconciliation(tmp_path, monkeypatch):
    from datetime import timedelta
    from decimal import Decimal

    from test_institutional_daemon_cycle import CycleHarness, execute_cycle

    monkeypatch.setenv("PRAMANA_RELEASE_REVISION", "a" * 40)
    env = environment(tmp_path)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert initialize(env)
    with pytest.raises(SpendRefused):
        from quant_ai.llm.spend import require_reservation
        require_reservation(PAYLOAD)
    h = CycleHarness(tmp_path)
    try:
        h.seed()
        assert execute_cycle(h).fill is not None  # Synthetic deterministic fixture.
        pid = h.programs.db.execute("SELECT program_id FROM execution_programs").fetchone()[0]
        h.now += timedelta(seconds=1)
        h.tick(Decimal(95))
        h.daemon.protection_tick(h.now)
        assert h.broker.get_positions("tenant") == ()
        assert len(h.broker.ledger_entries("tenant")) == 2
        assert h.runtime.reconcile_program(pid, tenant_id="tenant").program_state == "COMPLETE"
        assert h.journal.native_balance("tenant", "CASH_AVAILABLE", "INR") == h.broker.get_margin("tenant").cash_balance
    finally:
        h.close()
