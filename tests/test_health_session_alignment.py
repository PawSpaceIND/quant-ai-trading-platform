"""Real SQLite health reader with the existing session calendar; no market calls."""
import json
from datetime import datetime, timezone

import pytest
from test_protection_health import health_for

UTC = timezone.utc
INDIA_OPEN = datetime(2026, 9, 18, 5, tzinfo=UTC)
INDIA_EVENING = datetime(2026, 9, 18, 16, tzinfo=UTC)
USA_OPEN = datetime(2026, 9, 18, 15, tzinfo=UTC)


@pytest.fixture(autouse=True)
def clean_calendar_environment(monkeypatch):
    monkeypatch.delenv("PRAMANA_HOLIDAYS_JSON", raising=False)


def item(market="INDIA", exchange="NSE", fresh=False):
    return {"symbol": "SYNTHETIC", "market": market, "exchange": exchange,
            "fresh": fresh, "tickTimestamp": None}


def test_default_equity_holiday_does_not_report_a_dead_feed(tmp_path):
    now = datetime(2026, 1, 26, 5, tzinfo=UTC)
    result = health_for(tmp_path, [item()], now=now)
    assert result["status"] == "observation_ok"
    assert "market_data_stale_during_session" not in result["reasons"]
    assert result["market_data"]["state"] == "closed"


def test_environment_holidays_match_the_existing_daemon_parser(tmp_path, monkeypatch):
    monkeypatch.setenv("PRAMANA_HOLIDAYS_JSON", json.dumps({"INDIA": ["2026-09-18"]}))
    result = health_for(tmp_path, [item()], now=INDIA_OPEN)
    assert result["status"] == "observation_ok"
    assert result["market_data"]["state"] == "closed"


def test_exchange_only_holiday_does_not_close_another_exchange(tmp_path, monkeypatch):
    monkeypatch.setenv("PRAMANA_HOLIDAYS_JSON", json.dumps({"NSE": ["2026-09-18"]}))
    result = health_for(tmp_path, [item(), item(exchange="MCX")], now=INDIA_OPEN)
    assert result["market_data"]["open_instruments"] == 1
    assert result["market_data"]["state"] == "blind"
    assert result["status"] == "unhealthy"


@pytest.mark.parametrize("fresh_closed", [True, False])
def test_closed_usa_entry_cannot_hide_a_dead_open_india_feed(tmp_path, fresh_closed):
    result = health_for(tmp_path, [item(), item("USA", "NASDAQ", fresh_closed)], now=INDIA_OPEN)
    assert "market_data_stale_during_session" in result["reasons"]
    assert result["market_data"]["open_instruments"] == 1


@pytest.mark.parametrize("fresh_closed", [True, False])
def test_closed_india_entry_cannot_hide_a_dead_open_usa_feed(tmp_path, fresh_closed):
    result = health_for(tmp_path, [item(fresh=fresh_closed), item("USA", "NYSE")], now=USA_OPEN)
    assert result["status"] == "unhealthy"
    assert result["market_data"]["state"] == "blind"


def test_evening_commodity_session_uses_its_existing_exchange_clock(tmp_path):
    result = health_for(tmp_path, [item(exchange="MCX")], now=INDIA_EVENING)
    assert result["status"] == "unhealthy"
    assert result["market_data"]["state"] == "blind"


@pytest.mark.parametrize("exchange", ["NSE", "BSE", "NFO", "BFO"])
def test_evening_cash_or_equity_derivatives_are_closed(tmp_path, exchange):
    result = health_for(tmp_path, [item(exchange=exchange)], now=INDIA_EVENING)
    assert result["status"] == "observation_ok"
    assert result["market_data"]["state"] == "closed"


def test_one_stale_among_fresh_open_instruments_stays_partial_not_blind(tmp_path):
    result = health_for(tmp_path, [item(), item(fresh=True)], now=INDIA_OPEN)
    assert result["status"] == "observation_ok"
    assert result["market_data"]["state"] == "partial"
    assert result["market_data"]["fresh_open_instruments"] == 1


def test_fresh_open_feed_ignores_a_stale_closed_market(tmp_path):
    result = health_for(tmp_path, [item(fresh=True), item("USA", "NASDAQ")], now=INDIA_OPEN)
    assert result["market_data"]["state"] == "fresh"
    assert result["status"] == "observation_ok"


@pytest.mark.parametrize("entry", [item("GLOBAL", "LSE"), item("ATLANTIS"),
    {**item(), "exchange": []}, {**item(), "exchange": "PRIVATE_UNKNOWN_EXCHANGE"},
    {**item(), "fresh": "false"}, {**item(), "fresh": 0}])
def test_unresolvable_entry_is_unknown_not_a_crash_or_fresh_claim(tmp_path, entry):
    result = health_for(tmp_path, [entry], now=INDIA_OPEN)
    assert result["market_data"]["state"] == "unknown"
    assert "market_data_stale_during_session" not in result["reasons"]
    assert "PRIVATE_UNKNOWN_EXCHANGE" not in json.dumps(result)


@pytest.mark.parametrize("raw", ["not-json PRIVATE", "[]", "null", json.dumps({"INDIA": [7]}),
    json.dumps({"PRIVATE": []}), json.dumps({"INDIA": ["bad-date"]}), "x" * 65537])
def test_invalid_calendar_configuration_returns_fixed_unhealthy_reason(tmp_path, monkeypatch, raw):
    monkeypatch.setenv("PRAMANA_HOLIDAYS_JSON", raw)
    result = health_for(tmp_path, [item(fresh=True)], now=INDIA_OPEN)
    assert result["status"] == "unhealthy"
    assert "market_calendar_invalid" in result["reasons"]
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize("now", [datetime(2026, 9, 19, 5, tzinfo=UTC),
    datetime(2026, 9, 18, 3, 44, tzinfo=UTC), datetime(2026, 9, 18, 10, tzinfo=UTC)])
def test_weekend_and_regular_session_boundaries_do_not_create_false_feed_alarm(tmp_path, now):
    result = health_for(tmp_path, [item()], now=now)
    assert result["market_data"]["state"] == "closed"
    assert "market_data_stale_during_session" not in result["reasons"]


def test_existing_budget_sunday_special_session_is_preserved(tmp_path):
    result = health_for(tmp_path, [item()], now=datetime(2026, 2, 1, 5, tzinfo=UTC))
    assert result["status"] == "unhealthy"
    assert result["market_data"]["state"] == "blind"


def test_unknown_watchlist_is_not_labeled_fresh(tmp_path):
    result = health_for(tmp_path, [], now=INDIA_OPEN)
    assert result["market_data"]["state"] == "unknown"


def test_observation_does_not_expose_symbols_or_private_payload_fields(tmp_path):
    entry = {**item(fresh=True), "symbol": "PRIVATE_ACCOUNT_SYMBOL", "private": "PRIVATE_VALUE"}
    result = health_for(tmp_path, [entry], now=INDIA_OPEN)
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize("exchange,now", [("CDS", datetime(2026, 9, 18, 11, tzinfo=UTC)),
    ("MCX", datetime(2026, 9, 18, 18, 15, tzinfo=UTC))])
def test_other_existing_exchange_open_intervals_are_respected(tmp_path, exchange, now):
    result = health_for(tmp_path, [item(exchange=exchange)], now=now)
    assert result["market_data"]["state"] == "blind"


def test_us_default_holiday_is_closed(tmp_path):
    result = health_for(tmp_path, [item("USA", "NASDAQ")],
        now=datetime(2026, 7, 3, 15, tzinfo=UTC))
    assert result["market_data"]["state"] == "closed"
    assert result["status"] == "observation_ok"


@pytest.mark.parametrize("entries", [[item()] * 65, "SYNTHETIC", [None]])
def test_watchlist_loop_bound_and_unknown_state_are_explicit(tmp_path, entries):
    result = health_for(tmp_path, entries, now=INDIA_OPEN)
    assert result["market_data"]["state"] == "unknown"
    assert result["market_data"]["open_instruments"] is None


def test_exact_watchlist_bound_is_still_evaluated(tmp_path):
    result = health_for(tmp_path, [item()] * 64, now=INDIA_OPEN)
    assert result["market_data"]["open_instruments"] == 64
    assert result["market_data"]["state"] == "blind"


def test_missing_exchange_retains_existing_venue_default(tmp_path):
    entry = item()
    del entry["exchange"]
    result = health_for(tmp_path, [entry], now=INDIA_OPEN)
    assert result["market_data"]["state"] == "blind"


def test_sqlite_observation_preserves_payload_and_halt(tmp_path):
    import sqlite3

    from test_protection_health import valid, write

    from quant_ai.operations.health import protection_health

    dbpath = tmp_path / "state.sqlite"
    payload = valid(updatedAt=INDIA_OPEN.isoformat(), halted=True,
        haltReason="PRIVATE_REASON", watchlist=[item(fresh=True)])
    write(dbpath, payload, stamp=INDIA_OPEN.isoformat())
    before = dbpath.read_bytes()
    result = protection_health(dbpath, "pilot", now=INDIA_OPEN)
    assert result["status"] == "unhealthy" and "engine_halted" in result["reasons"]
    assert result["market_data"]["state"] == "fresh"
    assert dbpath.read_bytes() == before
    with sqlite3.connect(dbpath) as db:
        assert json.loads(db.execute("SELECT payload FROM pilot_runtime").fetchone()[0]) == payload
    assert "PRIVATE_REASON" not in json.dumps(result)


def test_current_cli_uses_existing_calendar_override_without_writing_state(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys
    from pathlib import Path

    from test_protection_health import valid, write

    now = datetime.now(UTC)
    monkeypatch.setenv("PRAMANA_HOLIDAYS_JSON", json.dumps({"INDIA": [now.date().isoformat()]}))
    dbpath = tmp_path / "state.sqlite"
    write(dbpath, valid(updatedAt=now.isoformat(), watchlist=[item()]), stamp=now.isoformat())
    before = dbpath.read_bytes()
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, str(root / "scripts/pilot_ops.py"),
        "health", "--database", str(dbpath), "--tenant", "pilot"], cwd=root,
        env={**os.environ, "PYTHONPATH": str(root / "src")}, capture_output=True, text=True, timeout=10, check=True)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["market_data"]["state"] == "closed"
    assert dbpath.read_bytes() == before
