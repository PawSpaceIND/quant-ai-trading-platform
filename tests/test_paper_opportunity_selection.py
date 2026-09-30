"""Offline synthetic ticks/catalog only; no subscriptions or provider requests."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from quant_ai.domain.models import AssetClass, Instrument, Market, Side
from quant_ai.execution.daemon import AutonomousTradingDaemon
from quant_ai.execution.opportunity_universe import PaperOpportunitySelector
from quant_ai.marketdata.ticker_stream import LiveTick

NOW = datetime(2026, 9, 22, 5, tzinfo=timezone.utc)


def instrument(symbol, tradable=True):
    return Instrument(symbol, Market.INDIA, AssetClass.EQUITY, "INR", "NSE", tradable=tradable)


def snapshot(choices=("B",), stamp=NOW, **changes):
    return {"schema": "pramana.research_shortlist.v1", "as_of": stamp.isoformat(),
            "target_session": "2026-09-22", "shortlist": list(choices),
            "names": [{"symbol": s, "eligible": True} for s in choices], **changes}


def selector(report=None, catalog=None, subscribed=("A", "B", "C")):
    quotes = {}
    selection = PaperOpportunitySelector(catalog or tuple(instrument(s) for s in "ABC"),
        paper_only=True, snapshot_provider=lambda now: snapshot() if report is None else report,
        tick_reader=quotes.get, subscribed_symbols=subscribed)
    return selection, quotes


def tick(symbol, at, **changes):
    return LiveTick(symbol=symbol, ltp=Decimal(100), volume=Decimal(10), bid=Decimal(99),
                    ask=Decimal(101), observed_at=at, source="zerodha", **changes)


def test_later_cycle_and_new_accepted_subscription_tick_required_holdings_retained():
    selection, quotes = selector()
    quotes["B"] = tick("B", NOW)
    assert [i.symbol for i in selection.select(NOW, ("A",))] == ["A"]
    assert selection.entry_symbols == frozenset()
    later = NOW + timedelta(minutes=10)
    assert [i.symbol for i in selection.select(later, ("A",))] == ["A"]
    quotes["B"] = tick("B", later)
    assert [i.symbol for i in selection.select(later, ("A",))] == ["A", "B"]
    assert selection.entry_issue("B", later) is None
    assert selection.entry_issue("C", later) == "paper_opportunity_not_admitted"
    assert selection.last_evidence["protection_universe"] == ["A", "B", "C"]
    assert selection.last_evidence["held_retained"] == ["A"]
    assert selection.catalog == tuple(instrument(s) for s in "ABC")


@pytest.mark.parametrize("report", [snapshot(()), snapshot(stamp=NOW - timedelta(days=2)),
                                    snapshot(stamp=NOW + timedelta(seconds=1)), {},
                                    snapshot(target_session="2026-09-23")])
def test_invalid_or_empty_scan_falls_back_to_fixed_catalog_with_fresh_prices(report):
    selection, quotes = selector(report)
    quotes["A"] = tick("A", NOW)
    assert [i.symbol for i in selection.select(NOW, ("C",))] == ["A", "C"]
    assert selection.last_evidence["status"] == "fixed_fallback"
    assert selection.entry_symbols == {"A"}
    assert selection.last_evidence["rejections"]["B"] == "fallback_price_not_fresh"


def test_scan_error_falls_back_and_does_not_grant_without_observed_ticks():
    selection, _ = selector()
    def error(now):
        raise RuntimeError("synthetic")
    selection.snapshot_provider = error
    assert selection.select(NOW, ()) == ()
    assert selection.last_evidence["status"] == "fixed_fallback"


def test_unknown_observation_only_and_unsubscribed_symbols_not_promoted():
    selection, quotes = selector(snapshot(("UNKNOWN", "B", "C")),
        catalog=(instrument("A"), instrument("B", False), instrument("C")), subscribed=("A", "B"))
    quotes["C"] = tick("C", NOW)
    assert selection.select(NOW, ()) == ()
    reasons = selection.last_evidence["rejections"]
    assert reasons["UNKNOWN"] == reasons["B"] == "unknown_or_ineligible_catalog_symbol"
    assert reasons["C"] == "subscription_not_configured"


@pytest.mark.parametrize("kind", ["stale", "future", "wrong_source", "invalid_price"])
def test_candidate_price_rechecked_at_entry_and_invalid_ticks_refuse(kind):
    selection, quotes = selector()
    selection.select(NOW, ())
    later = NOW + timedelta(minutes=10)
    quotes["B"] = tick("B", later)
    selection.select(later, ())
    assert selection.entry_issue("B", later) is None
    if kind == "stale": check_at = later + timedelta(seconds=61)
    else:
        check_at = later
        current = quotes["B"]
        quotes["B"] = LiveTick(current.symbol, Decimal(0) if kind == "invalid_price" else current.ltp,
            current.volume, current.bid, current.ask,
            later + timedelta(seconds=1) if kind == "future" else later,
            "synthetic" if kind == "wrong_source" else "zerodha")
    assert selection.entry_issue("B", check_at) == "paper_opportunity_not_admitted"


def test_mode_catalog_cap_and_missing_held_identity_refuse():
    with pytest.raises(ValueError, match="paper_only"):
        PaperOpportunitySelector((instrument("A"),), paper_only=False,
            snapshot_provider=None, tick_reader=None, subscribed_symbols=())
    with pytest.raises(ValueError, match="catalog_scope"):
        selector(catalog=tuple(instrument(f"S{i}") for i in range(51)))
    selection, _ = selector()
    with pytest.raises(ValueError, match="held_identity_missing"):
        selection.select(NOW, ("UNKNOWN",))
    assert not selection.entry_symbols


def test_pre_submit_adds_buy_gate_without_blocking_sell_path():
    selection, _ = selector()
    runner = SimpleNamespace(opportunity_selector=selection, apply_operator_halt=lambda: None,
        clock=lambda: NOW, check_protection_coverage=lambda now: False)
    assert AutonomousTradingDaemon._pilot_pre_submit(runner,
        SimpleNamespace(side=Side.BUY, symbol="B")) == "paper_opportunity_not_admitted"
    # SELL reaches the existing scope/session guard rather than the new BUY-only guard.
    runner.strategy_manifest = None
    runner.kill_switch = SimpleNamespace(engaged=True)
    runner.event_calendar = None
    runner.instruments = ()
    assert AutonomousTradingDaemon._pilot_pre_submit(runner,
        SimpleNamespace(side=Side.SELL, symbol="B")) == "pilot_session_or_scope_blocked"


def test_cadence_selection_never_replaces_protection_catalog_and_retains_holdings():
    selection, quotes = selector()
    later = NOW + timedelta(minutes=10)
    selection.select(NOW, ())
    quotes["B"] = tick("B", later)
    records = []
    runner = SimpleNamespace(opportunity_selector=selection, instruments=selection.catalog,
        instrument=selection.catalog[0], tenant_id="paper", audit=SimpleNamespace(append=lambda *args: records.append(args)),
        tracker=SimpleNamespace(broker=SimpleNamespace(get_positions=lambda tenant: (SimpleNamespace(symbol="C", quantity=1),))))
    chosen = AutonomousTradingDaemon._opportunity_decision_instruments(runner, later)
    assert [i.symbol for i in chosen] == ["B", "C"]
    assert [i.symbol for i in runner.instruments] == ["A", "B", "C"]
    assert records[0][0] == "paper_opportunity_selection"
    assert records[0][1]["held_retained"] == ["C"]
    selection.tick_reader = lambda symbol: None
    runner.tracker.broker.get_positions = lambda tenant: ()
    assert AutonomousTradingDaemon._opportunity_decision_instruments(runner, later) == (runner.instrument,)
    assert selection.entry_issue("A", later) == "paper_opportunity_not_admitted"


def test_snapshot_adapter_reuses_shortlist_and_caches_only_current_session():
    from test_research_shortlist import History

    from quant_ai.execution.opportunity_universe import CatalogResearchSnapshot
    from quant_ai.execution.session import MarketCalendar, default_holidays

    catalog = tuple(instrument(s) for s in "ABC")
    history = History()
    provider = CatalogResearchSnapshot(catalog, history, {s: s for s in "ABC"},
        MarketCalendar(holidays=default_holidays()))
    result = provider(NOW)
    assert result["previous_session"] == "2026-09-21"
    assert result["universe"] == ["A", "B", "C"]
    assert result["shortlist"]
    assert provider(NOW + timedelta(minutes=10)) is result
    assert len(history.clocks) == 3
    assert provider(NOW.replace(hour=0)) is None


@pytest.mark.parametrize("pilot,identity,ibkr", [(False, "legacy_cash", False),
                                               (True, "legacy_cash", False), (True, "bound_v1", True)])
def test_builder_refuses_wrong_modes_before_broker_or_provider_construction(monkeypatch, pilot, identity, ibkr):
    import quant_ai.daemon as factory

    monkeypatch.setenv("TRADING_LIVE_MONEY_ACTIVE", "false")
    monkeypatch.setattr(factory, "validate_identity_storage", lambda mode, **kwargs: mode)
    monkeypatch.setattr(factory, "PaperBrokerService", lambda *args, **kwargs: pytest.fail("broker opened"))
    with pytest.raises(ValueError, match="opportunity_bound_paper_nse_required"):
        factory.build_ghost_runner(paper_opportunity_selection=True, pilot_mode=pilot,
            order_identity_mode=identity, include_ibkr=ibkr, zerodha_api_key="synthetic",
            zerodha_access_token="synthetic", zerodha_instrument_tokens=(),
            zerodha_symbol_by_token={}, ib_client=None, ib_contracts=())


def test_default_factory_selection_is_disabled():
    import inspect

    from quant_ai.daemon import build_ghost_runner

    assert inspect.signature(build_ghost_runner).parameters["paper_opportunity_selection"].default is False


def test_next_session_requires_a_new_staging_cycle_and_tick():
    selection, quotes = selector()
    selection.select(NOW, ())
    later = NOW + timedelta(minutes=10)
    quotes["B"] = tick("B", later)
    selection.select(later, ())
    assert selection.entry_symbols == {"B"}
    tomorrow = NOW + timedelta(days=1)
    selection.snapshot_provider = lambda now: snapshot(stamp=tomorrow, target_session="2026-09-23")
    quotes["B"] = tick("B", tomorrow)
    assert selection.select(tomorrow, ()) == ()
    assert not selection.entry_symbols


def test_journal_preserves_selection_evidence_without_changing_proof_identity():
    import json

    from quant_ai.analytics.decision_journal import decision_row

    selection, _ = selector()
    selection.select(NOW, ("A",))
    proposal = SimpleNamespace(decision_id="proposal", symbol="A", market=Market.INDIA,
        asset_class=AssetClass.EQUITY, side=None, quantity=0, confidence=Decimal(0),
        expected_return=Decimal(0), expected_risk=Decimal(0), reference_price=Decimal(1),
        stop_price=None, take_profit_price=None, provenance={})
    result = SimpleNamespace(proposal=proposal, xai_trace=SimpleNamespace(decision_id="proof", input_matrix=(), provenance={}),
        fill=None, risk_decision=SimpleNamespace(approved=False, reason="synthetic"))
    row = decision_row(result, tenant_id="paper", now=NOW, opportunity_selection=selection.last_evidence)
    evidence = json.loads(row["funnel_evidence"])
    assert evidence["proof_decision_id"] == "proof"
    assert row["decision_id"] == "proposal"
    assert evidence["opportunity_selection"]["held_retained"] == ["A"]
    assert evidence["opportunity_selection"]["entry_admitted"] == []


def test_manifest_binds_static_selector_configuration_not_transient_admission():
    from quant_ai.execution.opportunity_universe import CatalogResearchSnapshot
    from quant_ai.governance.runtime_manifest import describe

    selection, _ = selector()
    selection.snapshot_provider = CatalogResearchSnapshot(selection.catalog, None, {"B": "sector"}, None)
    issues = []
    before = describe(selection, issues)
    assert issues == []
    selection.pending = {"B": (NOW, "2026-09-22")}
    selection.entry_symbols = {"B"}
    assert describe(selection, []) == before
    selection.subscribed = frozenset({"A"})
    assert describe(selection, []) != before


def test_actual_bound_paper_builder_keeps_subscriptions_identity_and_protection_catalog(tmp_path, monkeypatch):
    from quant_ai.daemon import build_ghost_runner
    from quant_ai.governance.directives import FounderDirectives

    monkeypatch.setenv("TRADING_LIVE_MONEY_ACTIVE", "false")
    catalog = tuple(instrument(s) for s in "ABC")
    runner = build_ghost_runner(zerodha_api_key="synthetic", zerodha_access_token="synthetic",
        zerodha_instrument_tokens=(1, 2, 3), zerodha_symbol_by_token={1: "A", 2: "B", 3: "C"},
        ib_client=None, ib_contracts=(), include_ibkr=False, pilot_mode=True, order_identity_mode="bound_v1",
        database=tmp_path / "paper.sqlite", oms_database=tmp_path / "oms.sqlite", tenant_id="paper",
        log_path=tmp_path / "events.jsonl", xai_directory=tmp_path / "proofs", halt_file=tmp_path / "HALT",
        directives=FounderDirectives(watchlist=catalog, sector_map={s: s for s in "ABC"}),
        paper_opportunity_selection=True)
    daemon = runner.daemon
    try:
        daemon.clock = lambda: NOW
        selection = daemon.opportunity_selector
        assert selection is not None
        assert daemon.telemetry is not None
        assert daemon.instruments == catalog
        assert runner.streams[0].instrument_tokens == (1, 2, 3)
        selection.snapshot_provider.day = NOW.date()
        selection.snapshot_provider.snapshot = snapshot()
        assert AutonomousTradingDaemon._opportunity_decision_instruments(daemon, NOW) == (catalog[0],)
        later = NOW + timedelta(minutes=10)
        daemon.clock = lambda: later
        assert daemon.tracker.market_feed.buffer.put(tick("B", later))
        assert AutonomousTradingDaemon._opportunity_decision_instruments(daemon, later) == (catalog[1],)
        assert daemon.instruments == catalog
        assert runner.streams[0].instrument_tokens == (1, 2, 3)
        assert not daemon.tracker.broker.ledger_entries("paper")
    finally:
        daemon.tracker.broker.close()
        daemon.scheduler.pipeline.runtime.oms.close()
