from copy import deepcopy
from datetime import datetime, timezone

import pytest

from quant_ai.execution.broker_reads import BrokerReadError
from quant_ai.execution.external_account_snapshot import capture_external_account

NOW = datetime(2026, 9, 11, 6, 30, tzinfo=timezone.utc)


def envelope(data):
    return {"status": "success", "data": data}


class Transport:
    def __init__(self):
        self.calls = []
        self.margins = envelope({"equity": {"enabled": True, "available": {"cash": 1000, "live_balance": 800, "opening_balance": 1200}, "net": 900}})
        self.positions = envelope({"net": [{"instrument_token": 123, "tradingsymbol": "INFY", "exchange": "NSE", "product": "CNC", "quantity": 2, "average_price": 100}]})
        self.profile = envelope({"user_id": "AB1234", "email": "PRIVATE_EMAIL_SENTINEL"})
        self.change = None

    def get(self, path):
        self.calls.append(path)
        value = {"/user/profile": self.profile, "/user/margins": self.margins,
                 "/portfolio/positions": self.positions}[path]
        result = deepcopy(value)
        if self.change:
            return self.change(path, result, self.calls)
        return result


def capture(transport):
    return capture_external_account(transport, "AB1234", "india-paper", clock=lambda: NOW)


def test_selected_account_snapshot_is_stable_and_private():
    transport = Transport()
    report = capture(transport)
    assert report["status"] == "consistent"
    assert [p["quantity"] for p in report["positions"]] == ["2"]
    assert report["funds"]["cash_balance"] == "1000"
    assert "AB1234" not in str(report) and "PRIVATE_EMAIL_SENTINEL" not in str(report)
    assert transport.calls == ["/user/profile", "/user/margins", "/portfolio/positions",
                               "/user/margins", "/portfolio/positions", "/user/profile"]
    assert report["positions"] == report["positionsBefore"]


def test_mid_capture_changes_and_missing_funds_are_not_consistent():
    transport = Transport()
    def change(path, value, calls):
        if path == "/portfolio/positions" and calls.count(path) == 2:
            value["data"]["net"][0]["quantity"] = 3
        return value
    transport.change = change
    assert capture(transport)["status"] == "changing"
    transport = Transport()
    transport.margins["data"]["equity"]["available"].pop("cash")
    assert capture(transport)["status"] == "incomplete"


def test_profile_change_rejected_and_no_snapshot_returned():
    transport = Transport()
    def change(path, value, calls):
        if path == "/user/profile" and calls.count(path) == 2:
            value["data"]["user_id"] = "OTHER"
        return value
    transport.change = change
    with pytest.raises(BrokerReadError):
        capture(transport)


def test_cross_day_or_slow_read_rejected():
    transport = Transport()
    times = iter([datetime(2026, 9, 11, 6, 30, tzinfo=timezone.utc),
                  datetime(2026, 9, 11, 6, 31, tzinfo=timezone.utc)])
    with pytest.raises(BrokerReadError):
        capture_external_account(transport, "AB1234", "india-paper", clock=lambda: next(times))


def test_capture_crossing_ist_midnight_is_rejected():
    transport = Transport()
    times = iter([datetime(2026, 9, 11, 18, 29, 55, tzinfo=timezone.utc),
                  datetime(2026, 9, 11, 18, 30, 5, tzinfo=timezone.utc)])
    with pytest.raises(BrokerReadError):
        capture_external_account(transport, "AB1234", "india-paper", clock=lambda: next(times))
