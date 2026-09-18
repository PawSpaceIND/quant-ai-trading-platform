"""Verify aggregate admission and conservative pre-request token reservations."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from quant_ai.llm.budget import SqliteAIBudget

NOW = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)


def budget(path, calls=10, tokens=100, clock=lambda: NOW):
    return SqliteAIBudget(path, daily_call_limit=calls, daily_token_limit=tokens, clock=clock)


def test_reservation_refuses_request_that_would_exceed_daily_token_headroom(tmp_path):
    ledger = budget(tmp_path / "threshold.sqlite")
    try:
        assert ledger.reserve("consensus", 99)
        ledger.record(
            "consensus",
            {"input_tokens": 50, "output_tokens": 49},
            token_reservation=99,
        )
        assert ledger.status("consensus")["remaining_tokens"] == 1
        assert not ledger.reserve("consensus", 2)
        assert ledger.reserve("consensus", 1)
        state = ledger.status("consensus")
        assert state["tokens"] == 99
        assert state["reserved_tokens"] == 1
        assert state["aggregate"]["reserved_tokens"] == 1
        assert state["remaining_tokens"] == 0
    finally:
        ledger.close()

def test_scopes_share_one_account_wide_call_ceiling(tmp_path):
    ledger = budget(tmp_path / "scopes.sqlite", calls=1)
    try:
        assert ledger.reserve("consensus")
        assert ledger.status("consensus")["calls"] == 1
        assert not ledger.reserve("headline_sentiment")
        headline = ledger.status("headline_sentiment")
        assert headline["calls"] == 0
        assert headline["aggregate"]["calls"] == 1
        assert headline["aggregate"]["remaining_calls"] == 0
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
        assert ledger.reserve("consensus", 25)
        ledger.record("consensus", None, token_reservation=25)
        state = ledger.status("consensus")
        assert state["calls"] == 1 and state["tokens"] == 0
        assert state["reserved_tokens"] == 25
        assert state["aggregate"]["reserved_tokens"] == 25
        assert state["exhausted"] and not ledger.reserve("consensus", 1)
    finally:
        ledger.close()


def test_prompt_cache_tokens_count_toward_daily_account_budget(tmp_path):
    ledger = budget(tmp_path / "usage.sqlite", tokens=4000)
    try:
        assert ledger.reserve("consensus", 3500)
        ledger.record(
            "consensus",
            {
                "input_tokens": 10,
                "output_tokens": 20,
                "cache_creation_input_tokens": 1000,
                "cache_read_input_tokens": 2000,
            },
            token_reservation=3500,
        )
        state = ledger.status("consensus")
        assert state["input_tokens"] == 3010
        assert state["output_tokens"] == 20
        assert state["tokens"] == 3030
        assert state["aggregate"]["tokens"] == 3030
        assert state["reserved_tokens"] == 0
        assert state["remaining_tokens"] == 970
    finally:
        ledger.close()


def test_legacy_scope_rows_backfill_account_aggregate_without_reset(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as db:
        db.execute(
            """
            CREATE TABLE ai_budget (
                day TEXT NOT NULL,
                scope TEXT NOT NULL,
                calls INTEGER NOT NULL DEFAULT 0,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (day, scope)
            )
            """
        )
        db.executemany(
            "INSERT INTO ai_budget VALUES (?, ?, ?, ?, ?)",
            [
                ("2026-09-18", "consensus", 2, 20, 10),
                ("2026-09-18", "headline_sentiment", 1, 5, 5),
            ],
        )
    ledger = budget(path, calls=3, tokens=100)
    try:
        state = ledger.status("consensus")
        assert state["aggregate"]["calls"] == 3
        assert state["aggregate"]["tokens"] == 40
        assert not ledger.reserve("consensus", 1)
        columns = {
            row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(ai_budget)")
        }
        assert "reserved_tokens" in columns
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
