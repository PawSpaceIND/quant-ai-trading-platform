"""Characterize current scoped thresholds, without claiming a hard aggregate cap."""
from __future__ import annotations

from datetime import datetime, timezone

from quant_ai.llm.budget import SqliteAIBudget

NOW = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)


def budget(path, calls=10, tokens=100, clock=lambda: NOW):
    return SqliteAIBudget(path, daily_call_limit=calls, daily_token_limit=tokens, clock=clock)


def test_record_full_consumption_then_refuse_following_admission(tmp_path):
    ledger = budget(tmp_path / "threshold.sqlite")
    try:
        assert ledger.reserve("consensus")
        ledger.record("consensus", {"input_tokens": 50, "output_tokens": 49})
        assert ledger.status("consensus")["remaining_tokens"] == 1
        assert ledger.reserve("consensus")
        ledger.record("consensus", {"input_tokens": 20, "output_tokens": 20})
        state = ledger.status("consensus")
        assert state["tokens"] == 139
        assert state["remaining_tokens"] == 0 and state["exhausted"]
        assert not ledger.reserve("consensus")
    finally:
        ledger.close()


def test_each_existing_scope_has_its_own_admission_counter(tmp_path):
    ledger = budget(tmp_path / "scopes.sqlite", calls=1)
    try:
        for scope in ("consensus", "headline_sentiment"):
            assert ledger.reserve(scope)
            assert ledger.status(scope)["calls"] == 1
            assert not ledger.reserve(scope)
        assert sum(ledger.status(s)["calls"] for s in ("consensus", "headline_sentiment")) == 2
    finally:
        ledger.close()


def test_utc_day_does_not_follow_the_ist_calendar_date(tmp_path):
    before = datetime.fromisoformat("2026-09-19T05:29:00+05:30")
    after = datetime.fromisoformat("2026-09-19T05:30:00+05:30")
    now = [before]
    ledger = budget(tmp_path / "day.sqlite", calls=1, clock=lambda: now[0])
    try:
        assert ledger.current_day() == "2026-09-18"
        assert ledger.reserve("consensus")
        assert not ledger.reserve("consensus")
        now[0] = after
        assert ledger.current_day() == "2026-09-19"
        assert ledger.reserve("consensus")
    finally:
        ledger.close()


def test_missing_usage_keeps_the_admitted_call_charged_to_its_scope(tmp_path):
    ledger = budget(tmp_path / "unknown.sqlite", calls=1)
    try:
        assert ledger.reserve("consensus")
        ledger.record("consensus", None)
        state = ledger.status("consensus")
        assert state["calls"] == 1 and state["tokens"] == 0
        assert state["exhausted"] and not ledger.reserve("consensus")
    finally:
        ledger.close()


def test_current_token_counter_is_not_a_cache_or_currency_meter(tmp_path):
    ledger = budget(tmp_path / "usage.sqlite")
    try:
        assert ledger.reserve("consensus")
        ledger.record("consensus", {"input_tokens": 10, "output_tokens": 20,
                                   "cache_creation_input_tokens": 1000,
                                   "cache_read_input_tokens": 2000})
        assert ledger.status("consensus")["tokens"] == 30
    finally:
        ledger.close()


def test_reopening_does_not_restore_consumed_daily_admissions(tmp_path):
    path = tmp_path / "durable.sqlite"
    first = budget(path, calls=1)
    assert first.reserve("consensus")
    first.close()
    reopened = budget(path, calls=1)
    try:
        assert reopened.status("consensus")["calls"] == 1
        assert not reopened.reserve("consensus")
    finally:
        reopened.close()
