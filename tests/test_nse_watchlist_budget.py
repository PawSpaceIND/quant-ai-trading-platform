"""Read-only budget observations use only offline, explicitly synthetic counters."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("inspect_budget", ROOT / "scripts/inspect_watchlist_budget.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
NOW = datetime(2026, 9, 18, 2, 45, tzinfo=timezone.utc)
ENV = {"TRADING_LIVE_MONEY_ACTIVE": "false"}


@pytest.fixture(autouse=True)
def offline_environment(monkeypatch):
    import socket
    def denied(*_args, **_kwargs):
        pytest.fail("Unexpected network call in offline budget verification")
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    for key in ("PRAMANA_AI_DAILY_CALL_LIMIT", "PRAMANA_AI_DAILY_TOKEN_LIMIT",
                "PRAMANA_AI_BUDGET_DB", "PRAMANA_LEDGER_PATH"):
        monkeypatch.delenv(key, raising=False)


def make_db(tmp_path, rows=(), *, unique=True):
    path = tmp_path / "budget.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE ai_budget (day TEXT, scope TEXT, calls INTEGER, "
                   "input_tokens INTEGER, output_tokens INTEGER"
                   + (", PRIMARY KEY(day,scope)" if unique else "") + ")")
        db.executemany("INSERT INTO ai_budget VALUES (?,?,?,?,?)", rows)
    return path


def observe(path, **kwargs):
    return module.observe_budget(path, **{"now": NOW, "days": 3, "environ": ENV, **kwargs})


def pick(report, day="2026-09-18", scope="consensus"):
    return next(row for row in report["usage"] if row["dayUTC"] == day and row["scope"] == scope)


def test_scopes_are_separate_and_real_limits_are_reused(tmp_path):
    path = make_db(tmp_path, [("2026-09-18", "consensus", 499, 1000, 200),
                             ("2026-09-18", "headline_sentiment", 12, 70, 30)])
    report = observe(path)
    assert pick(report)["remainingCallsAtSnapshot"] == 1
    assert pick(report, scope="headline_sentiment")["remainingCallsAtSnapshot"] == 488
    assert pick(report)["recordedTokens"] == 1200
    assert report["limitsPerScopePerUTCDate"]["PRAMANA_AI_DAILY_CALL_LIMIT"] == {
        "value": 500, "source": "runtime_default"}
    assert report["fiftyNameConsensus"]["completeSweepsWithinDailyCallLimit"] == 10
    assert report["currencyCost"] is report["forecastTokenDemand"] is None
    assert report["actualPaidRequestCount"] is report["measuredLatency"] is None
    assert report["deploymentApproved"] is report["budgetChanged"] is False


def test_aggregate_enabled_database_reports_shared_headroom_and_reservations(tmp_path):
    from quant_ai.llm.budget import SqliteAIBudget

    path = tmp_path / "aggregate.sqlite"
    ledger = SqliteAIBudget(
        path, daily_call_limit=500, daily_token_limit=2_000_000, clock=lambda: NOW
    )
    try:
        assert ledger.reserve("consensus", 200)
        ledger.record(
            "consensus",
            {"input_tokens": 100, "output_tokens": 50},
            token_reservation=200,
        )
        assert ledger.reserve("headline_sentiment", 75)
    finally:
        ledger.close()

    report = observe(path)
    assert report["aggregateEnforcementAvailable"] is True
    assert report["limitsAccountWidePerUTCDate"] == report["limitsPerScopePerUTCDate"]
    aggregate = next(
        row for row in report["aggregateUsage"] if row["dayUTC"] == "2026-09-18"
    )
    assert aggregate["reservedLogicalCalls"] == 2
    assert aggregate["recordedTokens"] == 150
    assert aggregate["reservedTokens"] == 75
    assert aggregate["remainingCallsAtSnapshot"] == 498
    assert aggregate["remainingTokenHeadroomAtSnapshot"] == 1_999_775
    assert "Aggregate ledger is active" in report["limitations"][0]


def test_existing_environment_parser_semantics_and_no_limit_change(tmp_path):
    env = {**ENV, "PRAMANA_AI_DAILY_CALL_LIMIT": "+0100", "PRAMANA_AI_DAILY_TOKEN_LIMIT": " 1000 "}
    original = dict(env)
    report = observe(make_db(tmp_path), environ=env)
    assert report["limitsPerScopePerUTCDate"]["PRAMANA_AI_DAILY_CALL_LIMIT"] == {
        "value": 100, "source": "environment"}
    assert report["fiftyNameConsensus"]["completeSweepsWithinDailyCallLimit"] == 2
    assert env == original


@pytest.mark.parametrize("flag", [None, "true", "yes", ""])
def test_paper_mode_must_be_explicit(tmp_path, flag):
    env = {} if flag is None else {"TRADING_LIVE_MONEY_ACTIVE": flag}
    with pytest.raises(module.BudgetObservationError, match="budget_paper_mode_required"):
        observe(make_db(tmp_path), environ=env)


@pytest.mark.parametrize("days", [True, 0, 8, -1, 1.5])
def test_window_is_bounded_and_not_boolean(tmp_path, days):
    with pytest.raises(module.BudgetObservationError, match="budget_window_invalid"):
        observe(make_db(tmp_path), days=days)


def test_clock_requires_timezone(tmp_path):
    with pytest.raises(module.BudgetObservationError, match="budget_aware_clock_required"):
        observe(make_db(tmp_path), now=NOW.replace(tzinfo=None))


@pytest.mark.parametrize("key", ["PRAMANA_AI_DAILY_CALL_LIMIT", "PRAMANA_AI_DAILY_TOKEN_LIMIT"])
@pytest.mark.parametrize("value", ["0", "-1", str(2**63), "SYNTHETIC_PRIVATE_BAD_LIMIT"])
def test_disabled_unusable_or_malformed_limits_refuse(tmp_path, key, value):
    with pytest.raises(module.BudgetObservationError, match="budget_limit_") as error:
        observe(make_db(tmp_path), environ={**ENV, key: value})
    assert value not in str(error.value)


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / "does-not-exist.sqlite"
    with pytest.raises(module.BudgetObservationError, match="budget_database_unavailable"):
        observe(path)
    assert not path.exists()


def test_database_connection_uses_read_only_mode(tmp_path, monkeypatch):
    path = make_db(tmp_path)
    connect = sqlite3.connect
    calls = []
    def checked(database_uri, **kwargs):
        calls.append(database_uri)
        assert database_uri.endswith("?mode=ro") and kwargs.get("uri") is True
        return connect(database_uri, **kwargs)
    monkeypatch.setattr(module.sqlite3, "connect", checked)
    observe(path)
    assert len(calls) == 1


def test_database_bytes_do_not_change(tmp_path):
    path = make_db(tmp_path, [("2026-09-18", "consensus", 4, 30, 20)])
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    observe(path)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_database_symlink_refused(tmp_path):
    path = make_db(tmp_path)
    link = tmp_path / "linked.sqlite"
    link.symlink_to(path)
    with pytest.raises(module.BudgetObservationError, match="budget_regular_database_required"):
        observe(link)


def test_real_table_required_not_fabricated_view(tmp_path):
    path = tmp_path / "view.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE VIEW ai_budget AS SELECT '2026-09-18' AS day, 'consensus' AS scope, "
                   "0 AS calls, 0 AS input_tokens, 0 AS output_tokens")
    with pytest.raises(module.BudgetObservationError, match="budget_table_unavailable"):
        observe(path)


@pytest.mark.parametrize("many", [False, True])
def test_duplicate_or_excess_rows_refused(tmp_path, many):
    rows = [("2026-09-18", "consensus", 0, 0, 0)] * (7 if many else 2)
    code = "budget_row_limit" if many else "budget_duplicate_or_excess_rows"
    with pytest.raises(module.BudgetObservationError, match=code):
        observe(make_db(tmp_path, rows, unique=False))


@pytest.mark.parametrize("column", [2, 3, 4])
@pytest.mark.parametrize("value", [-1, 1.5, "SYNTHETIC_PRIVATE_COUNTER"])
def test_invalid_counters_refuse_without_echo(tmp_path, column, value):
    row = ["2026-09-18", "consensus", 1, 2, 3]
    row[column] = value
    with pytest.raises(module.BudgetObservationError, match="budget_counters_invalid") as error:
        observe(make_db(tmp_path, [row]))
    assert "SYNTHETIC_PRIVATE" not in str(error.value)


def test_missing_rows_are_unknown_not_zero(tmp_path):
    report = observe(make_db(tmp_path, [("2026-09-18", "consensus", 0, 0, 0)]))
    assert pick(report)["rowPresent"] is True and pick(report)["recordedTokens"] == 0
    absent = pick(report, scope="headline_sentiment")
    assert absent["rowPresent"] is False
    assert all(absent[key] is None for key in (
        "reservedLogicalCalls", "recordedTokens", "remainingCallsAtSnapshot", "belowBothCountersAtSnapshot"))


def test_budget_day_remains_utc_across_india_midnight(tmp_path):
    now = datetime(2026, 9, 18, 0, 15, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    report = observe(make_db(tmp_path, [("2026-09-17", "consensus", 2, 10, 1)]), now=now, days=1)
    assert {row["dayUTC"] for row in report["usage"]} == {"2026-09-17"}
    assert report["checkedAt"].startswith("2026-09-17T18:45:")


def test_unrelated_scopes_and_old_dates_never_printed(tmp_path):
    report = observe(make_db(tmp_path, [("2026-09-18", "SYNTHETIC_PRIVATE_SCOPE", 10, 10, 10),
                                       ("2026-01-01", "consensus", 10, 10, 10)]))
    assert "SYNTHETIC_PRIVATE" not in json.dumps(report)
    assert not any(row["rowPresent"] for row in report["usage"])


@pytest.mark.parametrize("calls,inputs,outputs", [(500, 1, 1), (1, 2000000, 0), (700, 3000000, 0)])
def test_both_limits_and_nonnegative_headroom(tmp_path, calls, inputs, outputs):
    row = pick(observe(make_db(tmp_path, [("2026-09-18", "consensus", calls, inputs, outputs)])))
    assert row["belowBothCountersAtSnapshot"] is False
    assert row["remainingCallsAtSnapshot"] >= 0 and row["remainingRecordedTokensAtSnapshot"] >= 0


def test_cli_refuses_invalid_arguments_without_echo(capsys):
    assert module.main(["--days", "SYNTHETIC_PRIVATE_ARGUMENT"]) == 2
    output = capsys.readouterr().out
    assert "SYNTHETIC_PRIVATE" not in output
    assert json.loads(output)["reason"] == "budget_arguments_invalid"


def test_cli_refuses_missing_db_without_echoing_path(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TRADING_LIVE_MONEY_ACTIVE", "false")
    path = tmp_path / "SYNTHETIC_PRIVATE_PATH"
    assert module.main(["--database", str(path)]) == 2
    assert "SYNTHETIC_PRIVATE" not in capsys.readouterr().out
    assert not path.exists()


def test_cli_success_does_not_read_credential_env(tmp_path, monkeypatch, capsys):
    path = make_db(tmp_path)
    monkeypatch.setenv("TRADING_LIVE_MONEY_ACTIVE", "false")
    monkeypatch.setenv("ZERODHA_ACCESS_TOKEN", "SYNTHETIC_PRIVATE_CREDENTIAL")
    assert module.main(["--database", str(path), "--days", "1"]) == 0
    out = capsys.readouterr().out
    assert "SYNTHETIC_PRIVATE" not in out and str(path) not in out
    assert json.loads(out)["providerRequestsMade"] == 0


def test_malformed_selected_date_refuses(tmp_path):
    path = make_db(tmp_path, [("2026-09-17bad", "consensus", 1, 2, 3)])
    with pytest.raises(module.BudgetObservationError, match="budget_row_identity_invalid"):
        observe(path)


def test_query_only_untrusted_schema_transaction_and_work_limit(tmp_path, monkeypatch):
    path = make_db(tmp_path)
    callbacks = []
    queries = []
    connect = sqlite3.connect
    class ObservedConnection(sqlite3.Connection):
        def set_progress_handler(self, callback, instructions):
            callbacks.append((callback, instructions))
            super().set_progress_handler(callback, instructions)
        def execute(self, sql, *args):
            queries.append(sql)
            if sql.startswith("SELECT day,"):
                assert self.in_transaction
                assert super().execute("PRAGMA query_only").fetchone() == (1,)
                assert super().execute("PRAGMA trusted_schema").fetchone() == (0,)
            return super().execute(sql, *args)
    def observed_connect(*args, **kwargs):
        return connect(*args, **kwargs, factory=ObservedConnection)
    monkeypatch.setattr(module.sqlite3, "connect", observed_connect)
    observe(path)
    assert len(callbacks) == 1 and callbacks[0][1] == 1000
    callback = callbacks[0][0]
    assert any(callback() == 1 for _ in range(101))
    assert not any("INSERT" in sql or "UPDATE" in sql or "DELETE" in sql for sql in queries)


def test_cli_uses_explicit_budget_path_and_ledger_parent(tmp_path, monkeypatch, capsys):
    seen = []
    def fake_observe(path, **_kwargs):
        seen.append(path)
        return {"status": "synthetic_path_check"}
    monkeypatch.setattr(module, "observe_budget", fake_observe)
    monkeypatch.setenv("PRAMANA_AI_BUDGET_DB", str(tmp_path / "chosen.sqlite"))
    assert module.main([]) == 0
    monkeypatch.delenv("PRAMANA_AI_BUDGET_DB")
    monkeypatch.setenv("PRAMANA_LEDGER_PATH", str(tmp_path / "ledger.db"))
    assert module.main([]) == 0
    assert seen == [tmp_path / "chosen.sqlite", tmp_path / module.DEFAULT_DATABASE_NAME]
    assert str(tmp_path) not in capsys.readouterr().out


def test_cli_read_failure_details_redacted(monkeypatch, capsys):
    def broken(*_args, **_kwargs):
        raise RuntimeError("SYNTHETIC_PRIVATE_TRANSPORT_DETAIL")
    monkeypatch.setattr(module, "observe_budget", broken)
    assert module.main([]) == 2
    output = capsys.readouterr().out
    assert "SYNTHETIC_PRIVATE" not in output
    assert json.loads(output)["reason"] == "budget_observation_failed"


def test_stdin_execution_reads_no_account_secrets(tmp_path):
    import os
    import subprocess
    import sys
    path = make_db(tmp_path)
    child = subprocess.run(
        [sys.executable, "-B", "-", "--database", str(path), "--days", "1"],
        input=(ROOT / "scripts/inspect_watchlist_budget.py").read_text(),
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path),
             "PYTHONPATH": str(ROOT / "src"), "TRADING_LIVE_MONEY_ACTIVE": "false",
             "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, timeout=10, check=False)
    assert child.returncode == 0, child.stderr
    assert json.loads(child.stdout)["status"] == "observed"
    assert str(path) not in child.stdout


def test_sql_limits_rows_before_python_processing(tmp_path, monkeypatch):
    path = make_db(tmp_path, [("2026-09-18", "consensus", 0, 0, 0)] * 100, unique=False)
    connect = sqlite3.connect
    class BoundedCursor:
        def __init__(self, cursor):
            self.cursor = cursor
        def fetchall(self):
            rows = self.cursor.fetchall()
            assert len(rows) <= 7, "query returned more than its bounded observation window"
            return rows
    class ObservedConnection(sqlite3.Connection):
        def execute(self, sql, *args):
            cursor = super().execute(sql, *args)
            return BoundedCursor(cursor) if sql.startswith("SELECT day,") else cursor
    def observed_connect(*args, **kwargs):
        return connect(*args, **kwargs, factory=ObservedConnection)
    monkeypatch.setattr(module.sqlite3, "connect", observed_connect)
    with pytest.raises(module.BudgetObservationError, match="budget_row_limit"):
        observe(path)
