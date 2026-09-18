import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from quant_ai.operations.health import MAX_PAYLOAD, protection_health

NOW = datetime(2026, 9, 15, 6, tzinfo=timezone.utc)


def write(database, payload, stamp=None):
    stamp = NOW.isoformat() if stamp is None else stamp
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE IF NOT EXISTS pilot_runtime(tenant_id TEXT PRIMARY KEY, updated_at TEXT, payload TEXT)")
        db.execute("INSERT OR REPLACE INTO pilot_runtime VALUES (?,?,?)", ("pilot", stamp, json.dumps(payload)))


def valid(**changes):
    return {"mode": "paper", "status": "running", "halted": False, "updatedAt": NOW.isoformat(), **changes}


@pytest.mark.parametrize("changes,reason", [
    ({"status": "stopped"}, "engine_not_running"),
    ({"status": "faulted"}, "engine_not_running"),
    ({"mode": "live"}, "paper_mode_missing"),
    ({"halted": None}, "halt_state_unknown"),
    ({"halted": 0}, "halt_state_unknown"),
    ({"halted": "false"}, "halt_state_unknown"),
    ({"updatedAt": "invalid"}, "payload_heartbeat_stale_or_invalid"),
    ({"updatedAt": NOW.replace(tzinfo=None).isoformat()}, "payload_heartbeat_stale_or_invalid"),
    ({"updatedAt": (NOW - timedelta(seconds=16)).isoformat()}, "payload_heartbeat_stale_or_invalid"),
    ({"updatedAt": (NOW + timedelta(seconds=6)).isoformat()}, "payload_heartbeat_stale_or_invalid"),
    ({"updatedAt": (NOW - timedelta(seconds=1)).isoformat()}, "heartbeat_timestamp_mismatch"),
])
def test_fresh_storage_cannot_hide_invalid_engine_state(tmp_path, changes, reason):
    database = tmp_path / "ledger"
    write(database, valid(**changes))
    result = protection_health(database, "pilot", now=NOW)
    assert result["status"] == "unhealthy"
    assert reason in result["reasons"]
    assert result["heartbeat"] == "invalid_or_stale"


@pytest.mark.parametrize("offset", [-5, 0, 15])
def test_timestamp_boundaries_and_utc_equivalence(tmp_path, offset):
    database = tmp_path / "ledger"
    stamp = NOW - timedelta(seconds=offset)
    write(database, valid(updatedAt=stamp.astimezone(timezone(timedelta(hours=5, minutes=30))).isoformat()), stamp.isoformat())
    result = protection_health(database, "pilot", now=NOW)
    assert result["status"] == "observation_ok"
    assert result["age_seconds"] == offset
    assert result["observed_at"] == stamp.isoformat()


def test_voluntary_halt_keeps_liveness_but_fails_availability_without_disclosing_reason(tmp_path):
    database = tmp_path / "ledger"
    write(database, valid(halted=True, haltReason="private operator note", balance=100000))
    result = protection_health(database, "pilot", now=NOW)
    assert result["status"] == "unhealthy"
    assert result["heartbeat"] == "ok"
    assert result["reasons"] == ["engine_halted"]
    assert "private" not in json.dumps(result)
    assert "100000" not in json.dumps(result)
    assert protection_health(database, "other", now=NOW)["status"] == "unhealthy"


@pytest.mark.parametrize("payload", ["{secret", "null", "[]", "42", '"' + 'x' * MAX_PAYLOAD + '"'])
def test_corrupt_or_large_json_fails_without_echoing_contents(tmp_path, payload):
    database = tmp_path / "ledger"
    write(database, valid())
    with sqlite3.connect(database) as db:
        db.execute("UPDATE pilot_runtime SET payload=?", (payload,))
    result = protection_health(database, "pilot", now=NOW)
    assert "heartbeat_payload_invalid" in result["reasons"]
    assert len(json.dumps(result)) < 1000


def test_missing_database_is_not_created_and_corrupt_database_is_not_repaired(tmp_path):
    database = tmp_path / "ledger"
    assert "heartbeat_storage_unavailable" in protection_health(database, "pilot", now=NOW)["reasons"]
    assert not database.exists()
    database.write_bytes(b"not a sqlite database")
    assert protection_health(database, "pilot", now=NOW)["status"] == "unhealthy"
    assert database.read_bytes() == b"not a sqlite database"


def test_old_storage_and_cli_exit_codes(tmp_path):
    database = tmp_path / "ledger"
    now = datetime.now(timezone.utc)
    command = [sys.executable, str(Path(__file__).parents[1] / "scripts/pilot_ops.py"),
               "health", "--database", str(database), "--tenant", "pilot"]
    write(database, valid(updatedAt=now.isoformat()), now.isoformat())
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert json.loads(result.stdout)["status"] == "observation_ok"
    write(database, valid(updatedAt=now.isoformat()), "2000-01-01T00:00:00+00:00")
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 2
    assert "stored_heartbeat_stale_or_invalid" in json.loads(result.stdout)["reasons"]
    assert not result.stderr


def test_actual_pilot_telemetry_and_halt_recovery(tmp_path):
    """Halt and recovery through the real telemetry writer.

    The clock is the live one, as it is in the sibling test above, because the market feed
    judges a tick's staleness against wall-clock time rather than the timestamp it is
    handed. Pinned to a fixed past ``NOW`` the published tick was already refused as
    ``stale_live_tick``, so this ran with a watchlist that reported every instrument stale
    and nothing noticed - the health check did not read freshness until now.
    """
    from test_pilot_closure import publish_tick, runner_for
    runner = runner_for(tmp_path)
    broker = runner.daemon.tracker.broker
    now = datetime.now(timezone.utc)
    try:
        publish_tick(runner, "100", now)
        runner.daemon.protection_tick(now)
        assert protection_health(tmp_path / "ledger.db", "pilot", now=now)["status"] == "observation_ok"
        runner.daemon.halt_file.write_text("Health drill")
        runner.daemon.protection_tick(now)
        assert protection_health(tmp_path / "ledger.db", "pilot", now=now)["reasons"] == ["engine_halted"]
        runner.daemon.halt_file.unlink()
        runner.daemon.protection_tick(now)
        assert protection_health(tmp_path / "ledger.db", "pilot", now=now)["status"] == "observation_ok"
    finally:
        broker._connection.close()


# ------------------------------------------------- blind through an open session

OPEN_SESSION = NOW                                            # 11:30 IST, NSE regular hours
CLOSED_SESSION = datetime(2026, 9, 15, 14, tzinfo=timezone.utc)  # 19:30 IST, exchange shut


def entry(symbol: str, *, fresh: bool, market: str = "INDIA") -> dict:
    return {"symbol": symbol, "market": market, "fresh": fresh,
            "tickTimestamp": None if not fresh else NOW.isoformat()}


def health_for(tmp_path, watchlist, *, now=OPEN_SESSION):
    database = tmp_path / "ledger"
    # The payload's own clock has to match the one health is asked about, or the heartbeat
    # reads stale and the result says nothing about the watchlist.
    write(database, valid(watchlist=watchlist, updatedAt=now.isoformat()), stamp=now.isoformat())
    return protection_health(database, "pilot", now=now)


def test_a_whole_watchlist_stale_while_the_market_is_open_is_unhealthy(tmp_path):
    """The failure that ran for a full session while the container reported healthy.

    A dead feed does not stop the pilot. It keeps its heartbeat, keeps deciding, and every
    cadence records a well-formed abstention because no specialist has anything to say.
    Six and a quarter hours of that sat in the journal as 180 NEUTRAL rows, indistinguishable
    from considered restraint, and nothing anywhere said the engine could not see.

    The feed-loss halt already in the engine does not cover this: it fires when an open
    position cannot be priced, which is the right rule for protecting a position and no
    rule at all for a book that is flat.
    """
    result = health_for(tmp_path, [entry("INFY", fresh=False), entry("TCS", fresh=False)])

    assert "market_data_stale_during_session" in result["reasons"]
    assert result["status"] == "unhealthy"


def test_the_same_staleness_outside_market_hours_is_not_a_fault(tmp_path):
    """A closed exchange produces no ticks, and that is the normal state every night.

    Reporting it would make the container unhealthy from 15:30 to 09:15 daily, which is
    how a signal stops being read at all.
    """
    result = health_for(
        tmp_path,
        [entry("INFY", fresh=False), entry("TCS", fresh=False)],
        now=CLOSED_SESSION,
    )

    assert "market_data_stale_during_session" not in result["reasons"]
    assert result["status"] == "observation_ok"


def test_one_stale_symbol_among_fresh_ones_is_a_provider_gap_not_a_dead_feed(tmp_path):
    """A single symbol going quiet is ordinary; the whole watchlist at once is the feed.

    Alarming on one would fire on every thinly traded instrument and bury the case that
    matters.
    """
    result = health_for(tmp_path, [entry("INFY", fresh=True), entry("TCS", fresh=False)])

    assert "market_data_stale_during_session" not in result["reasons"]
    assert result["status"] == "observation_ok"


def test_a_watchlist_that_is_receiving_ticks_is_healthy(tmp_path):
    result = health_for(tmp_path, [entry("INFY", fresh=True), entry("TCS", fresh=True)])

    assert result["reasons"] == []
    assert result["status"] == "observation_ok"


@pytest.mark.parametrize("watchlist", [
    [],                                                   # nothing declared
    "INFY",                                               # not a list
    [{"symbol": "INFY", "market": "INDIA"}],              # no freshness reported
    [{"symbol": "INFY", "market": "ATLANTIS", "fresh": False}],  # market it cannot resolve
    [{"symbol": "INFY", "market": "X" * 40, "fresh": False}],    # oversized market name
    ["INFY"],                                             # entry is not an object
    [{"symbol": "INFY", "market": "INDIA", "fresh": False}] * 65,  # past the bound
])
def test_a_payload_it_cannot_read_never_raises_and_never_cries_wolf(tmp_path, watchlist):
    """The heartbeat is written by another process, so this parses it defensively.

    An unreadable watchlist is not evidence of a dead feed, and a health check that throws
    is a health check that reports nothing at all.
    """
    result = health_for(tmp_path, watchlist)

    assert "market_data_stale_during_session" not in result["reasons"]
