import importlib.util
import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from risk_history_fixture import fixture

from quant_ai.domain.models import Market
from quant_ai.execution.session import MarketCalendar, MarketState, default_holidays
from quant_ai.marketdata.risk_history import risk_history_input


def test_risk_fixture_reproduces_orthogonal_returns_with_real_calendar():
    result = fixture()
    stored = json.loads((Path(__file__).parent / "fixtures/historical-risk.json").read_text())
    assert result == stored
    assert result["expected"]["variance"] == pytest.approx(.13 * result["expected"]["covarianceDiagonal"])
    sessions = result["input"]["calendar"]["sessions"]
    assert "2026-01-15" not in sessions and "2026-03-31" not in sessions
    assert "2026-02-01" in sessions


def test_documented_budget_sunday_session_and_explicit_closure_override():
    calendar = MarketCalendar(default_holidays())
    moment = datetime(2026, 2, 1, 12, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert calendar.state(Market.INDIA, moment) == MarketState.REGULAR_HOURS
    assert calendar.state(Market.USA, moment) == MarketState.CLOSED
    assert calendar.state(Market.INDIA, moment.replace(hour=8)) == MarketState.CLOSED
    assert calendar.state(Market.INDIA, moment.replace(hour=15, minute=30)) == MarketState.POST_MARKET
    assert calendar.state(Market.INDIA, moment.replace(day=8)) == MarketState.CLOSED
    closed = MarketCalendar({Market.INDIA: frozenset({date(2026, 2, 1)})})
    assert closed.state(Market.INDIA, moment) == MarketState.CLOSED


def test_risk_producer_preserves_bad_data_but_excludes_current_and_future_dates():
    now = datetime(2026, 9, 14, 18, tzinfo=ZoneInfo("Asia/Kolkata"))
    rows = [{"instrument": {"symbol": "TCS"}, "history": [
        {"date": "2026-09-11T00:00:00+05:30", "close": 100},
        {"date": "2026-09-11", "close": -10},
        {"date": "invalid", "close": 20},
        {"date": "2026-09-14", "close": 110},
        {"date": "2026-09-15", "close": 115},
        {"date": "2025-12-31", "close": 99},
    ]}]
    value = risk_history_input(rows, now)
    assert value["calendar"]["sessions"][-1] == "2026-09-11"
    assert value["priceBasis"] == "provider_close_adjustments_unverified"
    assert value["instruments"][0]["observations"] == [
        {"date": "2026-09-11", "close": 100}, {"date": "2026-09-11", "close": -10}, {"date": "invalid", "close": 20}]
    assert risk_history_input(rows, now.replace(year=2027))["status"] == "unavailable"
    with pytest.raises(ValueError):
        risk_history_input(rows, now.replace(tzinfo=None))


def test_market_collector_publishes_risk_identity_and_long_history_without_external_calls(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("risk_collector_test", Path(__file__).parents[1] / "scripts/market_monitor.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    moment = datetime(2026, 9, 14, 12, tzinfo=ZoneInfo("Asia/Kolkata"))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz)

    calls = []

    class Kite:
        def __init__(self, **kwargs):
            pass

        def profile(self):
            return {"exchanges": ["NSE"]}

        def quote(self, symbols):
            return {"NSE:INFY": {"instrument_token": 123, "last_price": 100, "ohlc": {"close": 99}}}

        def historical_data(self, token, start, end, interval):
            calls.append((token, str(start), str(end), interval))
            return [{"date": moment.replace(day=11), "close": 99}, {"date": moment, "close": 100}]

    monkeypatch.setenv("ZERODHA_API_KEY", "synthetic")
    monkeypatch.setenv("ZERODHA_ACCESS_TOKEN", "synthetic")
    monkeypatch.setattr(module, "KiteConnect", Kite)
    monkeypatch.setattr(module, "datetime", Clock)
    monkeypatch.setattr(module, "CONFIG", tmp_path)
    monkeypatch.setattr("quant_ai.intelligence.external.rss.RssNewsSentimentAdapter.fetch", lambda *args: ())
    first = module.collect()
    second = module.collect()
    assert calls == [(123, "2026-01-01", "2026-09-14", "day")]
    assert first["riskHistory"] == second["riskHistory"]
    item = first["riskHistory"]["instruments"][0]
    assert item["providerInstrumentId"] == "123" and item["assetClass"] == "EQUITY"
    assert item["observations"] == [{"date": "2026-09-11", "close": 99}]
    assert len(first["riskHistory"]["calendar"]["sessions"]) > 100
