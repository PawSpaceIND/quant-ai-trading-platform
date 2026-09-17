"""Offline boundaries for the actual pilot book-risk components, not an order route."""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from test_pilot_required_risk_gates import (
    DIRECTIVES,
    NOW,
    SECTORS,
    SYMBOLS,
    bars,
    daily,
    gates,
    runner,
)
from test_portfolio_risk_controls import book, buy, plan

from quant_ai.domain.models import OrderIntent, Side
from quant_ai.notifications.trading import TradingNotificationDispatcher
from quant_ai.risk.book_history import DailyCloseHistory, normalize_sector_map, sector_map_from_env
from quant_ai.risk.policy import BookRiskFirewall
from quant_ai.risk.warden import RiskWarden


@pytest.fixture(autouse=True)
def isolated_inputs(monkeypatch):
    for name in ("PRAMANA_SECTOR_MAP_JSON", "PRAMANA_SECTOR_MAP_FILE", "PRAMANA_REQUIRE_BOOK_RISK_GATES",
                 "PRAMANA_BOOK_RISK_HISTORY", "PRAMANA_FOUNDER_DIRECTIVES_JSON", "PRAMANA_FOUNDER_DIRECTIVES_FILE"):
        monkeypatch.delenv(name, raising=False)


def entry():
    return OrderIntent("INFY", DIRECTIVES.watchlist[0].market, Side.BUY, 1, Decimal(100), "isolated-risk-test")


def test_noncallable_history_refuses_configuration_and_reports_unarmed(tmp_path):
    actual = runner(tmp_path, daily())
    risk = actual.daemon.scheduler.pipeline.runtime.warden.book_risk
    risk.history_provider = SimpleNamespace()
    assert risk.configuration_problem() == "required_book_history_missing"
    published = gates(actual)
    assert published["correlation_adjusted_gross"]["armed"] is False
    assert published["book_expected_shortfall"]["armed"] is False
    decision = risk.evaluate(entry(), book({}))
    assert not decision.approved
    assert decision.reason == "book_risk_measure_unavailable:required_book_history_missing"


def test_losing_both_required_inputs_never_reverts_to_optional_approval():
    risk = BookRiskFirewall(history_provider=lambda _: {}, sector_map=SECTORS, required_symbols=SYMBOLS)
    risk.history_provider = None
    risk.sector_map = {}
    assert not risk.armed  # The optional shortcut must still be overridden by required scope.
    decision = risk.evaluate(entry(), book({}))
    assert not decision.approved
    assert decision.reason == "book_risk_measure_unavailable:required_book_history_missing"


@pytest.mark.parametrize("seconds", [0, -1])
def test_history_freshness_budget_must_be_positive(seconds):
    with pytest.raises(ValueError, match="max_age_must_be_positive"):
        DailyCloseHistory(SimpleNamespace(), DIRECTIVES.watchlist, max_age=timedelta(seconds=seconds))


def test_history_clock_refuses_naive_even_without_any_records():
    adapted = DailyCloseHistory(SimpleNamespace(), DIRECTIVES.watchlist, max_age=timedelta(days=7))
    with pytest.raises(ValueError, match="clock_must_be_aware"):
        adapted._rows(DIRECTIVES.watchlist[0], (), NOW.replace(tzinfo=None))


def test_future_bar_after_same_days_close_is_not_accepted_as_closed_history():
    now = NOW.replace(hour=16)
    instrument = DIRECTIVES.watchlist[0]
    values = list(bars(instrument))
    values[-1] = replace(values[-1], timestamp=now + timedelta(minutes=30))
    adapted = DailyCloseHistory(SimpleNamespace(fetch=lambda *_: values), (instrument,),
                                clock=lambda: now, max_age=timedelta(days=7))
    with pytest.raises(ValueError, match="timestamp_invalid"):
        adapted((instrument.symbol,))


def test_failed_fetch_clears_previous_same_day_readiness():
    instrument = DIRECTIVES.watchlist[0]
    state = {"fail": False}
    def fetch(*_):
        if state["fail"]:
            raise OSError("synthetic unavailable history")
        return bars(instrument)
    adapted = DailyCloseHistory(SimpleNamespace(fetch=fetch), (instrument,),
                                clock=lambda: NOW, max_age=timedelta(days=7))
    adapted((instrument.symbol,))
    assert adapted.readiness((instrument.symbol,), NOW)["dataReady"] is True
    state["fail"] = True
    with pytest.raises(OSError):
        adapted((instrument.symbol,))
    assert adapted.readiness((instrument.symbol,), NOW)["dataReady"] is False


@pytest.mark.parametrize("offset", [timedelta(days=1), timedelta(minutes=-1)], ids=["prior-day", "future-observation"])
def test_fallback_observations_cannot_outlive_their_cache_boundary(offset):
    instrument = DIRECTIVES.watchlist[0]
    calls = []
    def fetch(*_):
        calls.append(1)
        return bars(instrument)
    adapted = DailyCloseHistory(SimpleNamespace(fetch=fetch), (instrument,),
                                clock=lambda: NOW, max_age=timedelta(days=7))
    adapted((instrument.symbol,))
    assert adapted.readiness((instrument.symbol,), NOW)["dataReady"] is True
    assert adapted.readiness((instrument.symbol,), NOW + offset)["dataReady"] is False
    assert len(calls) == 1


@pytest.mark.parametrize("exception", [OSError, RuntimeError])
def test_cached_read_failure_keeps_heartbeat_and_marks_history_unready(tmp_path, exception):
    provider = daily()
    for instrument in DIRECTIVES.watchlist:
        provider.fetch(instrument, NOW)
    actual = runner(tmp_path, provider)
    assert gates(actual)["book_expected_shortfall"]["dataReady"] is True
    calls = list(provider.feed.calls)
    def broken_cache(*_):
        raise exception("synthetic cached-source read error")
    provider.cached = broken_cache
    published = gates(actual)
    for key in ("correlation_adjusted_gross", "book_expected_shortfall"):
        assert published[key]["armed"] is True
        assert published[key]["dataReady"] is False
        assert published[key]["reason"] == "history_invalid_or_stale"
    assert provider.feed.calls == calls


@pytest.mark.parametrize("document", ['{"INFY":"IT","INFY":"OTHER"}', '{"INFY":null}', '[]'])
def test_file_sector_map_enforces_the_same_validation_as_inline(tmp_path, monkeypatch, document):
    path = tmp_path / "sectors.json"
    path.write_text(document)
    monkeypatch.setenv("PRAMANA_SECTOR_MAP_FILE", str(path))
    with pytest.raises((ValueError, TypeError)):
        sector_map_from_env()


def test_sector_map_keys_must_be_strings():
    with pytest.raises(TypeError, match="requires_string_pairs"):
        normalize_sector_map({123: "IT"})


def test_subtail_history_reports_exact_intervals_without_claiming_readiness():
    instrument = DIRECTIVES.watchlist[0]
    provider = SimpleNamespace(fetch=lambda *_: bars(instrument, count=100))
    adapted = DailyCloseHistory(provider, (instrument,), clock=lambda: NOW, max_age=timedelta(days=7))
    adapted((instrument.symbol,))
    result = adapted.readiness((instrument.symbol,), NOW)
    assert result["records"] == 100 and result["alignedIntervals"] == 99
    assert result["dataReady"] is False and result["reason"] == "insufficient_tail_history"


def test_warden_does_not_ignore_required_book_refusal():
    risk = BookRiskFirewall(history_provider=None, sector_map=SECTORS, required_symbols=SYMBOLS)
    warden = RiskWarden(TradingNotificationDispatcher(sinks=()), book_risk=risk)
    decision = warden.evaluate(buy("INFY", 1), plan(), book({}))
    assert not decision.approved
    assert decision.reason == "book_risk_measure_unavailable:required_book_history_missing"
