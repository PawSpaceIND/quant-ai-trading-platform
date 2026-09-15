from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from quant_ai.operations.zerodha_session import (
    INDIA_TZ,
    SessionRecord,
    is_expired,
    latest_cutoff,
    next_cutoff,
    read_session,
    write_session,
)

IST = ZoneInfo("Asia/Kolkata")


def ist(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=IST)


def utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def record(issued_at: datetime) -> SessionRecord:
    return SessionRecord("AB1234", "token-value", issued_at, None)


def test_india_timezone_is_asia_kolkata() -> None:
    assert INDIA_TZ.key == "Asia/Kolkata"
    assert ist(2026, 9, 15, 6).utcoffset() == timedelta(hours=5, minutes=30)


@pytest.mark.parametrize(
    ("issued_at", "now", "expired"),
    [
        # Same day, both after the cutoff: still valid.
        (ist(2026, 9, 15, 6, 30), ist(2026, 9, 15, 7), False),
        # Issued a minute before the cutoff, checked after it: dead.
        (ist(2026, 9, 15, 5, 59), ist(2026, 9, 15, 7), True),
        # Issued exactly at the cutoff is not "before" it.
        (ist(2026, 9, 15, 6, 0), ist(2026, 9, 15, 7), False),
        # Before the cutoff today, the relevant cutoff is yesterday's.
        (ist(2026, 9, 14, 7), ist(2026, 9, 15, 5, 30), False),
        (ist(2026, 9, 14, 5), ist(2026, 9, 15, 5, 30), True),
        # Checked at exactly 06:00 IST: the cutoff is now, so yesterday's token is dead.
        (ist(2026, 9, 14, 23, 59), ist(2026, 9, 15, 6, 0), True),
        (ist(2026, 9, 15, 6, 0), ist(2026, 9, 15, 6, 0), False),
        # Day boundary at midnight IST (18:30 UTC the previous day).
        (ist(2026, 9, 15, 6), ist(2026, 9, 16, 0, 10), False),
        (ist(2026, 9, 15, 5), ist(2026, 9, 16, 0, 10), True),
        # Old token, late check.
        (ist(2026, 9, 10, 9), ist(2026, 9, 15, 15), True),
        # Month and year boundaries.
        (ist(2026, 9, 30, 9), ist(2026, 10, 1, 5, 59), False),
        (ist(2026, 9, 30, 9), ist(2026, 10, 1, 6, 1), True),
        (ist(2026, 12, 31, 9), ist(2027, 1, 1, 5), False),
        (ist(2026, 12, 31, 9), ist(2027, 1, 1, 7), True),
    ],
)
def test_is_expired_against_most_recent_0600_ist_cutoff(
    issued_at: datetime, now: datetime, expired: bool
) -> None:
    assert is_expired(record(issued_at), now) is expired


@pytest.mark.parametrize(
    ("issued_at", "now", "expired"),
    [
        # 06:30 IST is 01:00Z; 05:30 IST is 00:00Z.
        (utc(2026, 9, 15, 0, 0), utc(2026, 9, 15, 1, 0), True),
        (utc(2026, 9, 15, 0, 30), utc(2026, 9, 15, 1, 0), False),
        # 23:00Z on the 14th is 04:30 IST on the 15th: before today's cutoff.
        (utc(2026, 9, 14, 23, 0), utc(2026, 9, 15, 4, 0), True),
        # 18:00Z on the 14th is 23:30 IST; checked at 20:00Z (01:30 IST the 15th): valid.
        (utc(2026, 9, 14, 18, 0), utc(2026, 9, 14, 20, 0), False),
        # Mixed zones compare as instants.
        (utc(2026, 9, 15, 0, 29), ist(2026, 9, 15, 6, 1), True),
    ],
)
def test_is_expired_accepts_utc_inputs(issued_at: datetime, now: datetime, expired: bool) -> None:
    assert is_expired(record(issued_at), now) is expired


def test_custom_cutoff_hour() -> None:
    issued = ist(2026, 9, 15, 7)
    assert is_expired(record(issued), ist(2026, 9, 15, 8, 30), cutoff_hour_ist=8) is True
    assert is_expired(record(issued), ist(2026, 9, 15, 8, 30), cutoff_hour_ist=6) is False
    with pytest.raises(ValueError):
        latest_cutoff(ist(2026, 9, 15, 8), cutoff_hour_ist=24)


def test_latest_and_next_cutoff_are_ist_aware() -> None:
    now = utc(2026, 9, 15, 10, 2)  # 15:32 IST
    latest = latest_cutoff(now)
    upcoming = next_cutoff(now)
    assert latest == ist(2026, 9, 15, 6)
    assert upcoming == ist(2026, 9, 16, 6)
    assert latest.tzinfo is INDIA_TZ and upcoming.tzinfo is INDIA_TZ
    assert next_cutoff(ist(2026, 9, 15, 6, 0)) == ist(2026, 9, 16, 6)
    assert next_cutoff(ist(2026, 9, 15, 5, 59)) == ist(2026, 9, 15, 6)


def test_naive_datetimes_are_rejected() -> None:
    with pytest.raises(ValueError, match="issued_at"):
        SessionRecord("AB1234", "token", datetime(2026, 9, 15, 7))  # noqa: DTZ001 - naive on purpose
    with pytest.raises(ValueError, match="now"):
        is_expired(record(ist(2026, 9, 15, 7)), datetime(2026, 9, 15, 8))  # noqa: DTZ001 - naive on purpose


def test_record_requires_identity_fields() -> None:
    with pytest.raises(ValueError, match="user_id"):
        SessionRecord("", "token", ist(2026, 9, 15, 7))
    with pytest.raises(ValueError, match="access_token"):
        SessionRecord("AB1234", " ", ist(2026, 9, 15, 7))


def test_from_kite_session_drops_everything_but_identity() -> None:
    issued = utc(2026, 9, 15, 3, 40)
    payload = {
        "user_id": "AB1234", "access_token": "tok", "api_key": "key", "public_token": "pub",
        "refresh_token": None, "login_time": ist(2026, 9, 15, 9, 10),
    }
    built = SessionRecord.from_kite_session(payload, issued_at=issued)
    assert built == SessionRecord("AB1234", "tok", issued, "2026-09-15T09:10:00+05:30")
    assert "api_key" not in built.__dict__ and "public_token" not in built.__dict__


def test_write_is_0600_and_round_trips_without_any_secret(tmp_path) -> None:
    path = tmp_path / "nested" / "zerodha-session.json"
    issued = datetime(2026, 9, 15, 3, 40, 5, tzinfo=timezone.utc)
    original = SessionRecord("AB1234", "access-token-value", issued, "2026-09-15 09:10:05")
    assert write_session(path, original) == path
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    text = path.read_text()
    assert "secret" not in text.lower()
    assert set(json.loads(text)) == {"user_id", "access_token", "issued_at", "login_time"}
    assert [item.name for item in path.parent.iterdir()] == [path.name]  # no temp file left
    restored = read_session(path)
    assert restored == original
    assert restored.issued_at.tzinfo is not None
    assert is_expired(restored, issued + timedelta(hours=1)) is False


def test_write_is_atomic_when_the_rename_fails(tmp_path, monkeypatch) -> None:
    path = tmp_path / "zerodha-session.json"
    write_session(path, record(ist(2026, 9, 15, 7)))
    before = path.read_text()

    def explode(source, destination):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", explode)
    with pytest.raises(OSError, match="disk full"):
        write_session(path, SessionRecord("AB1234", "newer-token", ist(2026, 9, 16, 7)))
    assert path.read_text() == before
    assert [item.name for item in tmp_path.iterdir()] == [path.name]


def test_write_replaces_an_existing_file_in_place(tmp_path) -> None:
    path = tmp_path / "zerodha-session.json"
    write_session(path, record(ist(2026, 9, 15, 7)))
    write_session(path, SessionRecord("AB1234", "newer-token", ist(2026, 9, 16, 7)))
    assert read_session(path).access_token == "newer-token"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"user_id": "AB1234", "access_token": "tok"}, "no issued_at"),
        ({"user_id": "AB1234", "access_token": "tok", "issued_at": "2026-09-15T09:10:00"}, "aware"),
        ({"user_id": "AB1234", "access_token": "tok", "issued_at": "yesterday"}, "unparseable"),
        ({"user_id": "", "access_token": "tok", "issued_at": "2026-09-15T03:40:00+00:00"}, "user_id"),
        ({"user_id": "AB1234", "issued_at": "2026-09-15T03:40:00+00:00"}, "access_token"),
        ([], "no issued_at"),
    ],
)
def test_read_session_fails_closed_on_incomplete_files(tmp_path, payload, message) -> None:
    path = tmp_path / "zerodha-session.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match=message):
        read_session(path)


def test_read_session_rejects_bad_json_and_missing_file(tmp_path) -> None:
    path = tmp_path / "zerodha-session.json"
    path.write_text("{not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        read_session(path)
    with pytest.raises(FileNotFoundError):
        read_session(tmp_path / "absent.json")


def test_read_session_accepts_zulu_suffix(tmp_path) -> None:
    path = tmp_path / "zerodha-session.json"
    path.write_text(json.dumps({"user_id": "AB1234", "access_token": "tok",
                                "issued_at": "2026-09-15T00:30:00Z"}))
    loaded = read_session(path)
    assert loaded.issued_at == utc(2026, 9, 15, 0, 30)
    assert loaded.login_time is None
