"""Real persisted health observations across the corrected MCX normal close.

Synthetic state only. These checks do not qualify holidays or actual feed health.
"""
from datetime import datetime, timedelta, timezone

import pytest
from test_mcx_seasonal_close import DAYS, IST
from test_protection_health import valid, write

from quant_ai.operations.health import protection_health


@pytest.mark.parametrize("day,close", DAYS, ids=["winter", "summer", "september", "december"])
@pytest.mark.parametrize("offset", [-1, 0, 1], ids=["before", "at", "after"])
@pytest.mark.parametrize("closed_nse_fresh", [False, True], ids=["nse-stale", "nse-fresh"])
def test_persisted_health_honors_mcx_exclusive_close(
    tmp_path, monkeypatch, day, close, offset, closed_nse_fresh
):
    monkeypatch.delenv("PRAMANA_HOLIDAYS_JSON", raising=False)
    now = datetime.combine(day, close, tzinfo=IST) + timedelta(microseconds=offset)
    watchlist = [
        {"symbol": "MCX-SYNTHETIC", "market": "INDIA", "exchange": "MCX", "fresh": False},
        {"symbol": "NSE-SYNTHETIC", "market": "INDIA", "exchange": "NSE", "fresh": closed_nse_fresh},
    ]
    database = tmp_path / "health.sqlite"
    write(database, valid(updatedAt=now.isoformat(), watchlist=watchlist), stamp=now.isoformat())
    before = database.read_bytes()
    for instant in (now, now.astimezone(timezone.utc)):
        result = protection_health(database, "pilot", now=instant)
        assert result["age_seconds"] == result["payload_age_seconds"] == 0
        assert result["heartbeat"] == ("invalid_or_stale" if offset < 0 else "ok")
        assert result["market_data"]["open_instruments"] == (1 if offset < 0 else 0)
        assert result["market_data"]["state"] == ("blind" if offset < 0 else "closed")
        assert result["status"] == ("unhealthy" if offset < 0 else "observation_ok")
        assert ("market_data_stale_during_session" in result["reasons"]) is (offset < 0)
        assert database.read_bytes() == before
