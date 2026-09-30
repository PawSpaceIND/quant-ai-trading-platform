"""The record that says whether the whole feed stalled or one name did, and why.

On 24 and 25 September 2026 the engine halted on ``protection_unreachable:COALINDIA``
mid-session and nobody could say afterwards whether the other eleven names had stalled
too, or whether COALINDIA's ticks had stopped arriving or arrived and been refused: the
halt names held symbols only, and the tick counters lived in memory until a restart.

Three pieces are pinned here: per-symbol tick counts in the buffer, one persisted minute
per in-session minute, and the operator's reading of those minutes (``feed-gaps``). All
three are evidence; none of them changes what the engine trades or when it halts.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from test_pilot_closure import publish_tick, runner_for

from quant_ai.execution import telemetry as telemetry_module
from quant_ai.marketdata import ticker_stream
from quant_ai.marketdata.ticker_stream import LiveTick, TickBuffer
from quant_ai.operations import feed_gaps as gaps

ROOT = Path(__file__).resolve().parents[1]
# Thursday 24 September 2026, 10:30 IST: NSE regular hours.
IN_SESSION = datetime(2026, 9, 24, 5, 0, tzinfo=timezone.utc)
SESSION = date(2026, 9, 24)


def tick(symbol: str, price: str, at: datetime, *, bid: str | None = None, ask: str | None = None) -> LiveTick:
    return LiveTick(symbol, Decimal(price), Decimal(100), Decimal(bid) if bid else None,
                    Decimal(ask) if ask else None, at, "test")


# ------------------------------------------------------------------ per-symbol counts


def test_the_buffer_counts_each_symbols_ticks_by_outcome() -> None:
    buffer = TickBuffer(clock=lambda: IN_SESSION)
    at = IN_SESSION - timedelta(seconds=5)
    assert buffer.put(tick("COALINDIA", "400", at))
    assert not buffer.put(tick("COALINDIA", "400", at))                       # duplicate
    assert not buffer.put(tick("COALINDIA", "401", at, bid="402", ask="401"))  # crossed
    assert not buffer.put(tick("COALINDIA", "401", at - timedelta(seconds=9)))  # out of order
    assert buffer.put(tick("NTPC", "350", at))
    assert buffer.symbol_counts(["COALINDIA", "NTPC", "TRENT"]) == {
        "COALINDIA": {"accepted": 1, "duplicate_tick": 1, "crossed_tick_quotes": 1, "out_of_order_tick": 1},
        "NTPC": {"accepted": 1},
        "TRENT": {},  # nothing arrived for it, which is itself the answer
    }
    # The process-wide totals are what they always were.
    assert buffer.integrity()["accepted"] == 2
    assert buffer.integrity()["rejected"] == {"duplicate_tick": 1, "crossed_tick_quotes": 1, "out_of_order_tick": 1}
    assert buffer.started_at == IN_SESSION


def test_an_uncurated_feed_cannot_grow_the_counts_without_bound(monkeypatch) -> None:
    monkeypatch.setattr(ticker_stream, "MAX_TRACKED_SYMBOLS", 2)
    buffer = TickBuffer(clock=lambda: IN_SESSION)
    for symbol in ("A", "B", "C", "D"):
        buffer.put(tick(symbol, "10", IN_SESSION))
    counts = buffer.symbol_counts(["A", "B", "C", ticker_stream.UNTRACKED])
    assert counts == {"A": {"accepted": 1}, "B": {"accepted": 1}, "C": {}, ticker_stream.UNTRACKED: {"accepted": 2}}


# ------------------------------------------------------------------ the persisted minute


def minutes_in(runner) -> list[tuple[str, dict]]:
    rows = runner.daemon.tracker.broker._connection.execute(
        "SELECT minute, payload FROM pilot_feed_minutes ORDER BY minute").fetchall()
    return [(minute, json.loads(payload)) for minute, payload in rows]


def ready(tmp_path, now: datetime):
    runner = runner_for(tmp_path)
    runner.daemon.clock = lambda: now
    return runner


def test_every_in_session_minute_records_each_watched_names_last_tick_and_counts(tmp_path) -> None:
    runner = ready(tmp_path, IN_SESSION)
    publish_tick(runner, "100", IN_SESSION - timedelta(seconds=3))
    runner.daemon.protection_tick(IN_SESSION + timedelta(seconds=20))
    ((minute, record),) = minutes_in(runner)
    assert minute == "2026-09-24T05:00:00+00:00"
    assert record["schema"] == "pramana.feed_minute.v1"
    assert record["countsSince"] == runner.daemon.tracker.market_feed.buffer.started_at.isoformat()
    assert record["symbols"] == {"INFY": {
        "fresh": True, "lastTickAt": (IN_SESSION - timedelta(seconds=3)).isoformat(), "ageSeconds": 23.0,
        "ticks": {"accepted": 1},
    }}
    # Written every second, kept once a minute: the last write of the minute stands.
    runner.daemon.protection_tick(IN_SESSION + timedelta(seconds=50))
    ((_, later),) = minutes_in(runner)
    assert later["symbols"]["INFY"]["ageSeconds"] == 53.0


def test_a_name_that_goes_quiet_is_recorded_stale_with_its_last_tick(tmp_path) -> None:
    runner = ready(tmp_path, IN_SESSION)
    publish_tick(runner, "100", IN_SESSION - timedelta(seconds=200))
    runner.daemon.protection_tick(IN_SESSION)
    ((_, record),) = minutes_in(runner)
    assert record["symbols"]["INFY"]["fresh"] is False
    assert record["symbols"]["INFY"]["ageSeconds"] == 200.0


def test_nothing_is_written_while_every_watched_market_is_closed(tmp_path) -> None:
    evening = datetime(2026, 9, 24, 14, 30, tzinfo=timezone.utc)  # 20:00 IST
    runner = ready(tmp_path, evening)
    publish_tick(runner, "100", evening - timedelta(seconds=2))
    runner.daemon.protection_tick(evening)
    assert minutes_in(runner) == []


def test_minutes_older_than_the_retention_are_pruned_once_a_day(tmp_path) -> None:
    runner = ready(tmp_path, IN_SESSION)
    db = runner.daemon.tracker.broker._connection
    old = (IN_SESSION - timedelta(days=telemetry_module.FEED_RETENTION_DAYS, minutes=1)).isoformat()
    kept = (IN_SESSION - timedelta(days=telemetry_module.FEED_RETENTION_DAYS - 1)).isoformat()
    with db:
        db.executemany("INSERT INTO pilot_feed_minutes VALUES (?,?,?)",
                       [("pilot", old, "{}"), ("pilot", kept, "{}")])
    publish_tick(runner, "100", IN_SESSION)
    runner.daemon.protection_tick(IN_SESSION)
    assert [minute for minute, in db.execute("SELECT minute FROM pilot_feed_minutes ORDER BY minute")] == [
        kept, "2026-09-24T05:00:00+00:00"]


def test_a_failed_feed_record_never_stops_the_heartbeat(tmp_path, monkeypatch, caplog) -> None:
    runner = ready(tmp_path, IN_SESSION)

    def explode(self, now):
        raise RuntimeError("disk full")

    monkeypatch.setattr(telemetry_module.PilotTelemetry, "_write_feed_minute", explode)
    publish_tick(runner, "100", IN_SESSION)
    runner.daemon.protection_tick(IN_SESSION)
    runtime = runner.daemon.tracker.broker._connection.execute("SELECT updated_at FROM pilot_runtime").fetchone()
    assert tuple(runtime) == (IN_SESSION.isoformat(),)
    assert "feed_minute_write_failed" in caplog.text


# ------------------------------------------------------------------ reading the minutes


def record(at: datetime, since: str = "boot-1", **symbols) -> tuple[datetime, dict]:
    """A minute: each symbol is (fresh, last tick, cumulative counts)."""
    return at, {"schema": "pramana.feed_minute.v1", "countsSince": since, "symbols": {
        name: {"fresh": fresh, "lastTickAt": last.isoformat() if last else None, "ticks": ticks}
        for name, (fresh, last, ticks) in symbols.items()}}


def minute(n: int) -> datetime:
    return IN_SESSION + timedelta(minutes=n)


def fresh(n: int, accepted: int) -> tuple[bool, datetime, dict]:
    return True, minute(n), {"accepted": accepted}


def test_one_names_refused_ticks_are_told_apart_from_a_feed_stall() -> None:
    """25 September's question: COALINDIA's ticks kept arriving and were thrown away."""
    last = minute(1) + timedelta(seconds=30)
    rows = [
        record(minute(0), COALINDIA=fresh(0, 100), NTPC=fresh(0, 90)),
        record(minute(1), COALINDIA=(True, last, {"accepted": 101}), NTPC=fresh(1, 91)),
        record(minute(2), COALINDIA=(True, last, {"accepted": 101, "crossed_tick_quotes": 20}), NTPC=fresh(2, 92)),
        record(minute(3), COALINDIA=(False, last, {"accepted": 101, "crossed_tick_quotes": 45}), NTPC=fresh(3, 93)),
        record(minute(4), COALINDIA=(False, last, {"accepted": 101, "crossed_tick_quotes": 70}), NTPC=fresh(4, 94)),
        record(minute(5), COALINDIA=fresh(5, 102), NTPC=fresh(5, 95)),
    ]
    built = gaps.report(rows, session_date=SESSION)
    assert built["whole_feed"] == []
    (gap,) = built["gaps"]
    # Counted from the minute of the last good tick, so the two minutes before the name
    # was declared stale are inside the count.
    assert (gap["symbol"], gap["minutes"], gap["ticks_during"], gap["verdict"]) == (
        "COALINDIA", 2, {"crossed_tick_quotes": 70}, gaps.REFUSED)
    text = gaps.render(built)
    assert "whole feed never stale at once" in text
    assert "COALINDIA" in text and "10:33-10:34" in text and "crossed_tick_quotes 70" in text
    assert "ticks arrived and every one was rejected" in text


def test_a_name_that_simply_stops_arriving_is_silent() -> None:
    last = minute(0)
    rows = [record(minute(0), COALINDIA=(True, last, {"accepted": 100}), NTPC=fresh(0, 1))] + [
        record(minute(n), COALINDIA=(n < 2, last, {"accepted": 100}), NTPC=fresh(n, 1 + n)) for n in range(1, 5)]
    (gap,) = gaps.report(rows, session_date=SESSION)["gaps"]
    assert (gap["verdict"], gap["ticks_during"]) == (gaps.SILENT, {})


def test_accepted_ticks_with_old_exchange_times_are_delayed_not_silent() -> None:
    last = minute(0)
    rows = [record(minute(0), COALINDIA=(True, last, {"accepted": 100}), NTPC=fresh(0, 1))] + [
        record(minute(n), COALINDIA=(n < 2, last, {"accepted": 100 + n}), NTPC=fresh(n, 1 + n)) for n in range(1, 5)]
    (gap,) = gaps.report(rows, session_date=SESSION)["gaps"]
    assert gap["verdict"] == gaps.DELAYED


def test_every_name_stale_at_once_is_a_whole_feed_stall() -> None:
    last = minute(0)
    rows = [record(minute(0), COALINDIA=fresh(0, 10), NTPC=fresh(0, 10))] + [
        record(minute(n), COALINDIA=(False, last, {"accepted": 10}), NTPC=(False, last, {"accepted": 10}))
        for n in range(3, 6)] + [record(minute(6), COALINDIA=fresh(6, 11), NTPC=fresh(6, 11))]
    built = gaps.report(rows, session_date=SESSION)
    assert built["whole_feed"] == [{"from": minute(3).isoformat(), "to": minute(5).isoformat(), "minutes": 3}]
    assert "WHOLE FEED 10:33-10:35" in gaps.render(built)


def test_a_restart_inside_a_gap_says_its_counts_do_not_compare() -> None:
    last = minute(0)
    rows = [record(minute(0), COALINDIA=(True, last, {"accepted": 500}), NTPC=fresh(0, 1)),
            record(minute(1), COALINDIA=(False, last, {"accepted": 500}), NTPC=fresh(1, 2)),
            record(minute(2), since="boot-2", COALINDIA=(False, last, {"accepted": 3}), NTPC=fresh(2, 3))]
    (gap,) = gaps.report(rows, session_date=SESSION)["gaps"]
    assert (gap["verdict"], gap["ticks_during"]) == (gaps.RESTART, None)
    # A restarted process can count past the old total. 500 then 900 is not 400 ticks
    # accepted during the gap; only the process start says the two counts are strangers.
    rows[2] = record(minute(2), since="boot-2", COALINDIA=(False, last, {"accepted": 900}), NTPC=fresh(2, 3))
    (gap,) = gaps.report(rows, session_date=SESSION)["gaps"]
    assert (gap["verdict"], gap["ticks_during"]) == (gaps.RESTART, None)


def test_a_missing_minute_ends_a_run_and_a_run_without_a_baseline_claims_nothing() -> None:
    last = minute(0) - timedelta(minutes=5)
    rows = [record(minute(0), COALINDIA=(False, last, {"accepted": 5})),
            record(minute(1), COALINDIA=(False, last, {"accepted": 5})),
            record(minute(4), COALINDIA=(False, last, {"accepted": 5}))]
    first, second = gaps.report(rows, session_date=SESSION)["gaps"]
    assert (first["minutes"], second["minutes"]) == (2, 1)
    assert first["verdict"] is None and "no count to compare against" in gaps.render(
        gaps.report(rows[:2], session_date=SESSION))


def test_websocket_events_are_that_sessions_and_carry_no_credential() -> None:
    lines = [
        json.dumps({"event": "websocket_disconnected", "generated_at": "2026-09-24T01:36:21+00:00",
                    "error": "KiteTicker closed 1006: connection was closed uncleanly"}),
        json.dumps({"event": "websocket_connect_failed", "generated_at": "2026-09-24T09:50:00+00:00",
                    "error": "failed wss://ws.kite.trade?api_key=abc&access_token=SECRET123 refused"}),
        json.dumps({"event": "websocket_disconnected", "generated_at": "2026-09-24T19:00:00+00:00", "error": "next day IST"}),
        json.dumps({"event": "xai_proof", "generated_at": "2026-09-24T05:00:00+00:00"}),
        "not json",
    ]
    found = gaps.stream_events(lines, SESSION)
    assert found[0] == "07:06 websocket_disconnected KiteTicker closed 1006: connection was closed uncleanly"
    assert found[1].startswith("15:20 websocket_connect_failed failed [redacted]")
    assert len(found) == 2
    assert "SECRET123" not in " ".join(found) and "api_key" not in " ".join(found)


# ------------------------------------------------------------------ the operator command


def pilot_ops():
    spec = importlib.util.spec_from_file_location("pilot_ops_feed_gaps", ROOT / "scripts/pilot_ops.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_feed_gaps_reads_the_ledger_and_log_without_writing(tmp_path) -> None:
    database, log = tmp_path / "pramana.db", tmp_path / "pramana-ghost.log"
    last = minute(0)
    rows = [record(minute(0), COALINDIA=(True, last, {"accepted": 5}), NTPC=fresh(0, 1)),
            record(minute(3), COALINDIA=(False, last, {"accepted": 5}), NTPC=fresh(3, 4))]
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE pilot_feed_minutes (tenant_id TEXT, minute TEXT, payload TEXT, PRIMARY KEY(tenant_id, minute))")
        db.execute("CREATE TABLE risk_control_state (tenant_id TEXT PRIMARY KEY, kill_switch_engaged INTEGER, "
                   "kill_switch_reason TEXT, updated_at TEXT)")
        db.executemany("INSERT INTO pilot_feed_minutes VALUES ('ghost', ?, ?)",
                       [(at.isoformat(), json.dumps(payload)) for at, payload in rows])
        db.execute("INSERT INTO risk_control_state VALUES ('ghost', 1, 'protection_unreachable:COALINDIA', ?)",
                   (minute(3).isoformat(),))
    log.write_text(json.dumps({"event": "websocket_disconnected", "generated_at": "2026-09-24T01:36:21+00:00",
                               "error": "KiteTicker closed 1006"}) + "\n")
    before = database.read_bytes()
    text = pilot_ops().feed_gaps(database, "ghost", session_date="2026-09-24", log_path=log)
    assert database.read_bytes() == before
    assert "halt       [1, 'protection_unreachable:COALINDIA', '2026-09-24T05:03:00+00:00']" in text
    assert "websocket  07:06 websocket_disconnected KiteTicker closed 1006" in text
    # The 10:30 minute held the last good tick, so it is the baseline even with minutes
    # missing between: the counts are cumulative, and nothing arrived since.
    assert "COALINDIA    10:33-10:33    1 min  last tick 10:30  ticks during: none  -> no tick arrived" in text
    empty = tmp_path / "old.db"
    sqlite3.connect(empty).close()
    assert "no feed minutes recorded for this session" in pilot_ops().feed_gaps(
        empty, "ghost", session_date="2026-09-24", log_path=tmp_path / "missing.log")


def test_the_command_is_registered(tmp_path) -> None:
    import subprocess
    import sys

    database = tmp_path / "pramana.db"
    sqlite3.connect(database).close()
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/pilot_ops.py"), "feed-gaps", "--database", str(database), "--date", "2026-09-24"],
        capture_output=True, text=True, check=False,
        env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("feed gaps  session 2026-09-24 (times IST)")

