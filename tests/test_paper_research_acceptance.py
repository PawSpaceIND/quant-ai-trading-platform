"""Synthetic cached bars/ticks and retrospective packages; no provider I/O."""
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from test_forecast_evaluation import NOW as SCORE_NOW
from test_forecast_evaluation import T, grants, package
from test_paper_opportunity_selection import NOW, instrument, tick
from test_rolling_forecast_evaluation import folds

from quant_ai.domain.models import Market
from quant_ai.execution.session import MarketCalendar, MarketState, default_holidays
from quant_ai.marketdata.models import Candle
from quant_ai.marketdata.timeframes import DailyHistoryProvider
from quant_ai.operations.paper_research_acceptance import (
    cached_research_coverage,
    evaluate_frozen_paper_research,
    qualify_paper_research,
)


def inputs():
    catalog = tuple(instrument(s) for s in "ABC")
    history = DailyHistoryProvider(None)
    calendar = MarketCalendar(holidays=default_holidays())
    day, sessions = NOW.astimezone(ZoneInfo("Asia/Kolkata")).date(), []
    while len(sessions) < 21:
        day -= timedelta(days=1)
        at = datetime.combine(day, time(12), ZoneInfo("Asia/Kolkata"))
        if calendar.state(Market.INDIA, at, exchange="NSE") == MarketState.REGULAR_HOURS:
            sessions.append(at)
    for item in catalog:
        bars = []
        for index, at in enumerate(reversed(sessions)):
            price = Decimal(100 + index)
            bars.append(Candle(item, at, price, price + 1, price - 1, price, Decimal(1000000)))
        history._cache[(item.symbol, item.market.value)] = (NOW.date(), tuple(bars))
    return catalog, history


def qualify(catalog, history, **changes):
    later = NOW + timedelta(minutes=10)
    options = {"now": NOW, "later": later, "previous_session": date(2026, 9, 21),
                   "sectors": {s: s for s in "ABC"}, "subscribed_symbols": ("A", "B", "C"),
                   "ticks": {s: tick(s, later) for s in "ABC"}, "held_symbols": ("C",)}
    options.update(changes)
    return qualify_paper_research(catalog, history, **options)


def test_complete_coverage_two_cycle_admission_and_held_protection_no_io(monkeypatch):
    catalog, history = inputs()
    monkeypatch.setattr(history, "fetch", lambda *a: pytest.fail("provider fetch forbidden"))
    result = qualify(catalog, history)
    assert result == qualify(catalog, history)
    assert result["coverage"]["eligible_stock_count"] == 3
    assert result["coverage"]["qualified_stock_count"] == 3
    assert result["first_cycle"]["entry_admitted"] == []
    assert set(result["entry_admitted"]) == set(result["research"]["shortlist"])
    assert result["later_cycle"]["held_retained"] == ["C"]
    assert result["later_cycle"]["protection_universe"] == list("ABC")
    assert result["fixed_comparator"] == list("ABC")
    assert result["activation_authorized"] is result["trading_authorized"] is False


@pytest.mark.parametrize("change,reason", [("stale", "current_day_cache_absent"),
    ("short", "insufficient_prior_sessions"), ("missing", "missing_previous_session"),
    ("identity", "cached_history_invalid_or_error")])
def test_cache_refusals_are_per_symbol_and_force_deterministic_fallback(change, reason):
    catalog, history = inputs()
    key = ("A", catalog[0].market.value)
    stamp, bars = history._cache[key]
    if change == "stale": stamp -= timedelta(days=1)
    if change == "short": bars = bars[-10:]
    if change == "missing": bars = bars[:-1]
    if change == "identity": bars = (replace(bars[0], instrument=catalog[1]), *bars[1:])
    history._cache[key] = (stamp, bars)
    result = qualify(catalog, history)
    assert not result["coverage"]["complete"]
    assert reason in result["coverage"]["names"][0]["reasons"]
    assert result["research"] is None
    assert result["later_cycle"]["status"] == "fixed_fallback"
    assert result["selected_names"] == list("ABC")


def test_unknown_reader_never_called_and_observation_only_not_promoted():
    class Unknown:
        def cached(self, *args):
            pytest.fail("unknown cached implementation forbidden")
    report, bars = cached_research_coverage((instrument("A"), instrument("B", False)), Unknown(),
        now=NOW, previous_session=date(2026, 9, 21))
    assert report["catalog_count"] == 2
    assert report["eligible_stock_count"] == 1
    assert report["names"][0]["reasons"] == ["unsupported_cached_reader"]
    assert not bars


def test_subscription_and_tick_refusals_do_not_remove_holdings():
    catalog, history = inputs()
    result = qualify(catalog, history, subscribed_symbols=("A",), ticks={})
    assert result["entry_admitted"] == []
    assert result["later_cycle"]["held_retained"] == ["C"]
    assert result["later_cycle"]["rejections"]["B"] == "subscription_not_configured"


@pytest.mark.parametrize("changes", [{"paper_only": False}, {"later": NOW},
    {"later": NOW + timedelta(days=1)}, {"held_symbols": ("UNKNOWN",)}])
def test_mode_clock_and_held_identity_boundaries(changes):
    with pytest.raises(ValueError):
        qualify(*inputs(), **changes)


def score(raw, **changes):
    options = {"source_grants": grants(), "folds": folds(), "frozen_at": T,
                   "run_id": "synthetic", "candidate_id": "synthetic", "clock": lambda: SCORE_NOW}
    options.update(changes)
    return evaluate_frozen_paper_research(raw, **options)


def test_frozen_after_cost_acceptance_reuses_all_folds_and_refuses_result_selection(tmp_path):
    result = score(package(tmp_path, count=120))
    assert result["evaluation"]["candidate"]["samples"] == 80
    assert all(result["pooled_better_than_baseline"].values())
    assert not result["activation_authorized"]
    assert not result["fixed_vs_dynamic_strategy_evaluated"]
    with pytest.raises(ValueError, match="research_plan_must_precede"):
        score(package(tmp_path / "late", count=120), frozen_at=folds()[0]["holdout_start"])


def test_cost_losses_do_not_become_acceptance_or_execution_pnl(tmp_path):
    result = score(package(tmp_path, count=120, costs=".1"))
    assert result["evaluation"]["candidate"]["positive_rate"] == "0"
    assert not all(result["pooled_better_than_baseline"].values())
    assert not result["portfolio_returns_evaluated"]


def test_future_and_target_day_bars_cannot_supply_missing_previous_history():
    catalog, history = inputs()
    key = ("A", catalog[0].market.value)
    stamp, bars = history._cache[key]
    history._cache[key] = (stamp, (*bars[:-1], replace(bars[-1], timestamp=NOW + timedelta(days=1))))
    result = qualify(catalog, history)
    reasons = result["coverage"]["names"][0]["reasons"]
    assert reasons == ["insufficient_prior_sessions", "missing_previous_session"]


def test_calendar_mismatch_and_catalog_cap_are_refused():
    with pytest.raises(ValueError, match="calendar_previous_session"):
        qualify(*inputs(), previous_session=date(2026, 9, 20))
    with pytest.raises(ValueError, match="catalog_scope"):
        qualify(tuple(instrument(f"S{i}") for i in range(51)), None)


def test_unequal_folds_pool_every_after_cost_opportunity_once(tmp_path):
    boundaries = folds()
    boundaries[0]["holdout_end"] = T + timedelta(minutes=140)
    result = score(package(tmp_path, count=120), folds=boundaries)
    assert [f["candidate"]["samples"] for f in result["evaluation"]["folds"]] == [30, 40]
    assert result["evaluation"]["candidate"]["samples"] == 70


def test_history_coverage_does_not_imply_strategy_or_sector_eligibility():
    result = qualify(*inputs(), sectors={})
    assert result["coverage"]["complete"]
    assert result["research"]["shortlist"] == []
    assert all(row["reasons"] == ["unknown_sector"] for row in result["research"]["names"])
    assert result["later_cycle"]["status"] == "fixed_fallback"


def test_stale_ticks_never_become_admission_with_good_history():
    result = qualify(*inputs(), ticks={s: tick(s, NOW - timedelta(seconds=61)) for s in "ABC"})
    assert result["coverage"]["complete"]
    assert result["entry_admitted"] == []
    assert set(result["later_cycle"]["rejections"].values()) == {"await_later_cycle_observed_subscription_price"}


def test_cached_reader_error_is_explicit_without_fetch_or_private_error_text(monkeypatch):
    catalog, history = inputs()
    def fail(*args):
        raise OSError("private provider message")
    monkeypatch.setattr(history, "cached", fail)
    report, bars = cached_research_coverage(catalog, history, now=NOW, previous_session=date(2026, 9, 21))
    assert not bars
    assert all(r["reasons"] == ["cached_history_invalid_or_error"] for r in report["names"])
    assert "private provider message" not in str(report)


def test_nonfinite_history_is_not_qualified():
    catalog, history = inputs()
    key = ("A", catalog[0].market.value)
    stamp, bars = history._cache[key]
    history._cache[key] = (stamp, (*bars[:-1], replace(bars[-1], high=Decimal("Infinity"))))
    assert qualify(catalog, history)["coverage"]["names"][0]["reasons"] == ["cached_history_invalid_or_error"]
