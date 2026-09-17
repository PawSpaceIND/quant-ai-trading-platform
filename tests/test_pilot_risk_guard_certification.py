"""Synthetic checks of actual pilot risk guards. No broker/provider/LLM calls."""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
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

from quant_ai import daemon
from quant_ai.risk.book_history import DailyCloseHistory, normalize_sector_map, sector_map_from_env
from quant_ai.risk.policy import BookRiskFirewall


@pytest.fixture(autouse=True)
def clean_risk_environment(monkeypatch):
    for name in ("PRAMANA_SECTOR_MAP_JSON", "PRAMANA_SECTOR_MAP_FILE", "PRAMANA_REQUIRE_BOOK_RISK_GATES",
                 "PRAMANA_BOOK_RISK_HISTORY", "PRAMANA_FOUNDER_DIRECTIVES_JSON", "PRAMANA_FOUNDER_DIRECTIVES_FILE"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("source", [None, object()])
def test_required_history_must_be_callable(source):
    risk = BookRiskFirewall(history_provider=source, sector_map=SECTORS, required_symbols=SYMBOLS)
    assert risk.configuration_problem() == "required_book_history_missing"


@pytest.mark.parametrize("missing", ["all", "TCS"])
def test_required_mapping_coverage_is_checked_directly(missing):
    mapping = {} if missing == "all" else {k: v for k, v in SECTORS.items() if k != missing}
    risk = BookRiskFirewall(history_provider=lambda _: {}, sector_map=mapping, required_symbols=SYMBOLS)
    expected = ",".join(sorted(SYMBOLS)) if missing == "all" else "TCS"
    assert risk.configuration_problem() == "required_sector_mapping_missing:" + expected


@pytest.mark.parametrize("delta", [timedelta(0), timedelta(days=-1)])
def test_freshness_budget_must_be_positive(delta):
    with pytest.raises(ValueError, match="^book_history_max_age_must_be_positive$"):
        DailyCloseHistory(SimpleNamespace(), DIRECTIVES.watchlist, max_age=delta)


def test_history_rows_refuse_a_naive_clock_even_for_empty_input():
    adapted = DailyCloseHistory(SimpleNamespace(), DIRECTIVES.watchlist, max_age=timedelta(days=7))
    with pytest.raises(ValueError, match="^book_history_clock_must_be_aware$"):
        adapted._rows(DIRECTIVES.watchlist[0], (), NOW.replace(tzinfo=None))


@pytest.mark.parametrize("kind", ["naive", "future"])
def test_timestamp_guard_has_its_own_refusal_before_session_validation(kind):
    instrument = DIRECTIVES.watchlist[0]
    rows = list(bars(instrument))
    stamp = NOW.replace(tzinfo=None) if kind == "naive" else NOW + timedelta(days=1)
    rows[-1] = replace(rows[-1], timestamp=stamp)
    adapted = DailyCloseHistory(SimpleNamespace(), (instrument,), max_age=timedelta(days=7))
    with pytest.raises(ValueError, match="^book_history_timestamp_invalid$"):
        adapted._rows(instrument, rows, NOW)


def test_failed_refetch_clears_previous_successful_observation():
    instrument = DIRECTIVES.watchlist[0]
    provider = SimpleNamespace(fetch=lambda *_: bars(instrument))
    adapted = DailyCloseHistory(provider, (instrument,), clock=lambda: NOW, max_age=timedelta(days=7))
    adapted((instrument.symbol,))
    assert adapted.readiness((instrument.symbol,), NOW)["dataReady"] is True
    def broken(*_):
        raise ValueError("synthetic fetch failure")
    provider.fetch = broken
    with pytest.raises(ValueError, match="synthetic fetch failure"):
        adapted((instrument.symbol,))
    report = adapted.readiness((instrument.symbol,), NOW)
    assert report["dataReady"] is False and report["records"] == 0


@pytest.mark.parametrize("observed_at", [NOW - timedelta(days=1), NOW + timedelta(seconds=1)])
def test_observation_fallback_refuses_another_day_or_future_read(observed_at):
    instrument = DIRECTIVES.watchlist[0]
    adapted = DailyCloseHistory(SimpleNamespace(fetch=lambda *_: bars(instrument)), (instrument,),
                                clock=lambda: observed_at, max_age=timedelta(days=7))
    adapted._observed[instrument.symbol] = (observed_at, bars(instrument))
    report = adapted.readiness((instrument.symbol,), NOW)
    assert report["dataReady"] is False and report["records"] == 0


@pytest.mark.parametrize("source", ["inline", "file"])
def test_duplicate_sector_keys_are_refused_in_both_environment_sources(tmp_path, monkeypatch, source):
    raw = '{"INFY":"IT","INFY":"OTHER"}'
    if source == "inline":
        monkeypatch.setenv("PRAMANA_SECTOR_MAP_JSON", raw)
    else:
        path = tmp_path / "groups.json"
        path.write_text(raw)
        monkeypatch.setenv("PRAMANA_SECTOR_MAP_FILE", str(path))
    with pytest.raises(ValueError, match="^sector_map_duplicate_symbol$"):
        sector_map_from_env()


@pytest.mark.parametrize("mapping,reason", [([], "sector_map_must_be_object"),
    ({"INFY": None}, "sector_map_requires_string_pairs"),
    ({1: "IT"}, "sector_map_requires_string_pairs")])
def test_grouping_type_checks_have_specific_refusals(mapping, reason):
    with pytest.raises(TypeError, match="^" + reason + "$"):
        normalize_sector_map(mapping)


def test_incomplete_sector_map_is_not_reported_armed_in_required_runtime(tmp_path):
    actual = runner(tmp_path, daily())
    risk = actual.daemon.scheduler.pipeline.runtime.warden.book_risk
    risk.sector_map.pop("TCS")
    observed = gates(actual)
    sector = observed["sector_concentration"]
    assert sector["armed"] is False and sector["dataReady"] is False
    assert sector["coveredSymbols"] == 4 and sector["required"] is True
    assert observed["correlation_adjusted_gross"]["armed"] is True


def test_noncallable_provider_is_not_reported_armed(tmp_path):
    actual = runner(tmp_path, daily())
    risk = actual.daemon.scheduler.pipeline.runtime.warden.book_risk
    risk.history_provider = object()
    observed = gates(actual)
    assert observed["correlation_adjusted_gross"]["armed"] is False
    assert observed["book_expected_shortfall"]["armed"] is False


@pytest.mark.parametrize("missing", ["history", "map"])
def test_missing_required_inputs_refuse_before_analysis_pipeline(tmp_path, monkeypatch, missing):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Required risk inputs were missing but analysis assembly was reached")
    monkeypatch.setattr(daemon, "SwarmMarketAnalysisPipeline", forbidden)
    with pytest.raises(ValueError, match="^pilot_risk_gates_unarmed:"):
        runner(tmp_path, None if missing == "history" else daily(), {} if missing == "map" else SECTORS)


def test_required_mode_never_returns_optional_approval_after_all_inputs_disappear():
    from decimal import Decimal

    from test_portfolio_risk_controls import book

    from quant_ai.domain.models import OrderIntent, Side

    risk = BookRiskFirewall(history_provider=lambda _: {}, sector_map=SECTORS, required_symbols=SYMBOLS)
    risk.history_provider = None
    risk.sector_map = {}
    order = OrderIntent("INFY", DIRECTIVES.watchlist[0].market, Side.BUY, 1, Decimal(100), "synthetic")
    result = risk.evaluate(order, book({}))
    assert result.approved is False
    assert result.reason == "book_risk_measure_unavailable:required_book_history_missing"
