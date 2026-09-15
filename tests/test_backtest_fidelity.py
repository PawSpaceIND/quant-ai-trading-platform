"""The backtester must test the system that trades.

Before these tests the replay built its runtime as ``SwarmPaperTradingService(broker=...,
xai_logger=...)`` and took the permissive default for everything else: no position cap,
no blocked asset class, an unarmed book firewall, an Atlas with no founder instructions.
The ghost daemon passed all of those. Every tearsheet the project had ever produced was
therefore drawn by a strictly more permissive engine than the one that trades - a result
that is worse than none, because it is believed.

What is held here is the property, not the wiring: the configuration the replay runs and
the configuration the daemon runs must be equal, and the handful of things a replay
honestly cannot reproduce must be written down, recorded on the run and visible on the
sheet.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from test_friction_replay import _dataset

from quant_ai.agents.swarm import TradeProposal
from quant_ai.agents.swarm_runtime import SwarmPaperTradingService
from quant_ai.agents.traded_runtime import (
    DECISION_MAKER_NOTES,
    DETERMINISTIC_CONSENSUS,
    GOVERNED_ATTRIBUTES,
    LLM_CONSENSUS,
    build_traded_runtime,
    policy_attributes,
    runtime_configuration,
)
from quant_ai.analytics import decision_journal as journal
from quant_ai.backtesting.replay import (
    TRADED_CONFIGURATION_DIFFERENCES,
    HistoricalMarketDataFeed,
    HistoricalReplayHarness,
)
from quant_ai.backtesting.tearsheet import build_tearsheet
from quant_ai.daemon import build_ghost_runner
from quant_ai.domain.models import AssetClass, Instrument, Market, RiskMode, Side
from quant_ai.governance.directives import FounderDirectives
from quant_ai.governance.event_calendar import EventCalendar, ScheduledEvent
from quant_ai.llm.anthropic_client import AnthropicSwarmClient
from quant_ai.planning.capital import CapitalGoalEngine, CapitalPlanRequest

AAPL = Instrument("AAPL", Market.USA, AssetClass.EQUITY, "USD", "NASDAQ")
# The replay datasets start here; the blackout calendar below covers the same day.
WINDOW_DAY = date(2026, 9, 8)


def _plan():
    return CapitalGoalEngine().recommend(
        CapitalPlanRequest(
            Decimal(100000), Decimal("0.8"), Decimal("0.2"),
            expected_edge=Decimal("0.02"), requested_mode=RiskMode.BALANCED,
        )
    )


def _directives(**overrides) -> FounderDirectives:
    """Directives that actually bite, so an ignored knob cannot pass unnoticed."""
    base = {
        "max_open_positions": 2,
        "allowed_asset_classes": frozenset({AssetClass.EQUITY, AssetClass.INDEX}),
        "sector_map": {"AAPL": "TECH"},
        "instructions": "Preserve capital first.",
        "watchlist": (AAPL,),
    }
    return FounderDirectives(**{**base, **overrides})


def _ghost_runtime(tmp_path, monkeypatch, directives, *, name="ghost", llm_client=None):
    monkeypatch.setenv("TRADING_LIVE_MONEY_ACTIVE", "false")
    for variable in ("PRAMANA_SECTOR_MAP_JSON", "PRAMANA_SECTOR_MAP_FILE"):
        monkeypatch.delenv(variable, raising=False)
    runner = build_ghost_runner(
        zerodha_api_key="test-key",
        zerodha_access_token="test-token",
        zerodha_instrument_tokens=(1,),
        zerodha_symbol_by_token={1: "AAPL"},
        ib_client=SimpleNamespace(),
        ib_contracts=(),
        include_ibkr=False,
        database=tmp_path / f"{name}.db",
        log_path=tmp_path / f"{name}.log",
        xai_directory=tmp_path / f"{name}-xai",
        directives=directives,
        instrument=AAPL,
        llm_client=llm_client,
    )
    return runner.daemon.scheduler.pipeline.runtime


def _harness(tmp_path, monkeypatch, directives, *, name="replay", **overrides):
    from quant_ai.execution.paper_ledger import PaperBrokerService

    for variable in ("PRAMANA_SECTOR_MAP_JSON", "PRAMANA_SECTOR_MAP_FILE"):
        monkeypatch.delenv(variable, raising=False)
    broker = PaperBrokerService(tmp_path / f"{name}.db", starting_capital=Decimal(100000))
    return HistoricalReplayHarness(
        broker, _plan(), quantity=10, directives=directives, **overrides
    )


# --------------------------------------------------------------- the drift is closed


def test_the_replay_runs_the_configuration_the_daemon_trades(tmp_path, monkeypatch):
    directives = _directives()
    ghost = runtime_configuration(_ghost_runtime(tmp_path, monkeypatch, directives))
    replay = runtime_configuration(_harness(tmp_path, monkeypatch, directives).build_runtime())
    assert replay == ghost
    # Named individually, because an equal-dicts assertion that quietly compared two
    # empty dicts would also pass.
    assert replay["components"]["runtime"]["parameters"]["max_open_positions"] == 2
    assert replay["components"]["warden"]["parameters"]["book_risk_armed"] == ["sector_map"]
    assert "COMMODITY" in replay["components"]["warden"]["parameters"]["blocked_asset_classes"]
    assert (
        replay["components"]["atlas"]["parameters"]["founder_instructions"]
        == "Preserve capital first."
    )


def test_a_knob_that_reaches_only_the_daemon_fails_the_check(tmp_path, monkeypatch):
    """The guard earns its place only if a real divergence trips it."""
    directives = _directives()
    replay = runtime_configuration(_harness(tmp_path, monkeypatch, directives).build_runtime())
    widened = (
        replace(directives, max_open_positions=9),
        replace(directives, allowed_asset_classes=frozenset(AssetClass)),
        replace(directives, sector_map={}),
        replace(directives, instructions="Swing for the fences."),
    )
    for index, wider in enumerate(widened):
        ghost = runtime_configuration(
            _ghost_runtime(tmp_path, monkeypatch, wider, name=f"wider-{index}")
        )
        assert ghost != replay


def test_the_only_gap_to_the_live_decision_maker_is_the_declared_one(tmp_path, monkeypatch):
    directives = _directives()
    # A client with an injected transport: enough for the daemon to prefer the model, and
    # no network anywhere near the test.
    live = runtime_configuration(
        _ghost_runtime(
            tmp_path, monkeypatch, directives,
            llm_client=AnthropicSwarmClient(client=SimpleNamespace()),
        )
    )
    replay = runtime_configuration(_harness(tmp_path, monkeypatch, directives).build_runtime())
    assert live["decisionMaker"] == LLM_CONSENSUS
    assert replay["decisionMaker"] == DETERMINISTIC_CONSENSUS
    assert {
        name for name in live["components"] if live["components"][name] != replay["components"][name]
    } == {"llm"}
    assert {
        item["knob"] for item in TRADED_CONFIGURATION_DIFFERENCES
    } >= {"decisionMaker", "attribution"}


def test_every_policy_knob_on_the_service_is_fingerprinted():
    """A control added to the service must be compared, or the drift check goes blind."""
    assert policy_attributes(SwarmPaperTradingService()) == GOVERNED_ATTRIBUTES


# ------------------------------------------------------- the traded knobs are enforced


def test_the_founder_scope_stops_a_replayed_entry_the_old_default_would_have_taken(
    tmp_path, monkeypatch
):
    permissive = _harness(tmp_path, monkeypatch, FounderDirectives(), name="permissive")
    traded = _harness(
        tmp_path, monkeypatch,
        _directives(allowed_asset_classes=frozenset({AssetClass.INDEX}), watchlist=()),
        name="scoped",
    )
    assert permissive.run(_dataset()).order_ids
    scoped = traded.run(_dataset())
    assert scoped.order_ids == ()
    assert scoped.equity_curve[-1] == Decimal(100000)


def test_the_founder_position_cap_reaches_the_replayed_runtime(tmp_path, monkeypatch):
    harness = _harness(tmp_path, monkeypatch, _directives(max_open_positions=3))
    assert harness.build_runtime().max_open_positions == 3


def test_a_scheduled_event_blacks_out_a_replayed_entry_and_never_an_exit(tmp_path, monkeypatch):
    calendar = EventCalendar(
        (ScheduledEvent(WINDOW_DAY, WINDOW_DAY, "earnings", "AAPL"),), timezone="UTC"
    )
    harness = _harness(tmp_path, monkeypatch, _directives(), event_calendar=calendar)
    result = harness.run(_dataset())
    # The control: the same window and the same directives without the calendar trades,
    # so the empty run above is the blackout and not an unrelated veto.
    assert _harness(tmp_path, monkeypatch, _directives(), name="open-day").run(_dataset()).order_ids
    assert result.order_ids == ()
    assert result.equity_curve[-1] == Decimal(100000)
    # Entries only, exactly as the pilot path treats a blackout.
    assert harness._blackout_veto(_proposal(Side.BUY)) == "event_blackout:earnings"
    assert harness._blackout_veto(_proposal(Side.SELL)) is None


def _proposal(side: Side) -> TradeProposal:
    return TradeProposal(
        "fidelity", "AAPL", Market.USA, "USA", AssetClass.EQUITY, side, 1, Decimal(100),
        Decimal(95), Decimal(110), Decimal("0.8"), Decimal("0.01"), Decimal("0.005"), (),
    )


def test_the_replay_starts_every_specialist_unscored(tmp_path, monkeypatch):
    """The declared attribution difference is a deliberate refusal, not an oversight."""
    harness = _harness(tmp_path, monkeypatch, _directives())
    journal.insert_decision(harness.broker, {
        "decision_id": "closed-1", "tenant_id": harness.tenant_id, "symbol": "AAPL",
        "market": "USA", "asset_class": "EQUITY",
        "decided_at": datetime(2026, 9, 1, tzinfo=timezone.utc).isoformat(),
        "stance": "BUY", "side": "BUY", "confidence": "0.7", "expected_return": "0.01",
        "expected_risk": "0.005", "reference_price": "100", "stop_price": "95",
        "take_profit_price": "110", "regime": "trending_up", "mode": "llm",
        "governance": "filled", "reason": None, "order_id": "closed-1",
        "agents": json.dumps({"technical": "BUY"}), "realized_net_pnl": "250",
    })
    assert harness.build_runtime().attribution.weight_for("technical") == (
        Decimal(1), "unscored",
    )
    # The same builder does restore when a caller can bound the outcomes to its window.
    restored = build_traded_runtime(
        broker=harness.broker,
        directives=_directives(),
        attribution_journal_tenant=harness.tenant_id,
    )
    assert restored.attribution.weight_for("technical")[1] == "blended"


# ------------------------------------------------ the result says what produced it


def test_the_run_evidence_and_the_tearsheet_name_the_decision_maker(tmp_path, monkeypatch):
    database = tmp_path / "labelled.db"
    harness = _harness(tmp_path, monkeypatch, _directives(), name="labelled")
    result = harness.run(_dataset())
    sheet = json.loads(build_tearsheet(result, harness.broker).to_json())
    harness.broker.close()

    assert result.decision_maker == DETERMINISTIC_CONSENSUS
    assert sheet["decision_maker"] == DETERMINISTIC_CONSENSUS
    assert "NOT a backtest of the LLM" in sheet["decision_maker_note"]
    assert sheet["traded_configuration_differences"] == [
        dict(item) for item in TRADED_CONFIGURATION_DIFFERENCES
    ]

    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute(
            "SELECT metadata FROM paper_replay_runs WHERE run_id=?", (result.replay_run_id,)
        ).fetchone()
    configuration = json.loads(row[0])["configuration"]
    assert configuration["decisionMaker"] == DETERMINISTIC_CONSENSUS
    assert configuration["decisionMakerNote"] == DECISION_MAKER_NOTES[DETERMINISTIC_CONSENSUS]
    assert configuration["tradedConfigurationDifferences"] == [
        dict(item) for item in TRADED_CONFIGURATION_DIFFERENCES
    ]
    # The recorded configuration is the one that ran, not a claim made alongside it.
    assert configuration["tradedRuntime"]["components"]["runtime"]["parameters"] == {
        "allow_position_scaling": False, "max_open_positions": 2,
    }


def test_every_declared_difference_carries_a_reason():
    for item in TRADED_CONFIGURATION_DIFFERENCES:
        assert set(item) == {"knob", "traded", "replayed", "reason"}
        assert len(item["reason"]) > 80 and item["traded"] != item["replayed"]


def test_replaying_the_llm_decision_maker_is_refused_rather_than_faked(tmp_path, monkeypatch):
    from quant_ai.execution.paper_ledger import PaperBrokerService

    del monkeypatch
    with pytest.raises(ValueError, match=f"replay_decision_maker_unsupported:{LLM_CONSENSUS}"):
        HistoricalReplayHarness(
            PaperBrokerService(tmp_path / "refused.db"), _plan(), decision_maker=LLM_CONSENSUS
        )


# --------------------------------------------------- the guard survives the tightening


def test_the_lookahead_guard_still_fires_under_the_traded_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr(
        HistoricalMarketDataFeed, "fetch_ohlcv",
        lambda self, instrument, start, end, timeframe="1m": self.bars,
    )
    harness = _harness(
        tmp_path, monkeypatch, _directives(), name="guarded",
        event_calendar=EventCalendar(
            (ScheduledEvent(WINDOW_DAY, WINDOW_DAY, "earnings", "AAPL"),), timezone="UTC"
        ),
    )
    with pytest.raises(RuntimeError, match="lookahead_violation: future bar"):
        harness.run(_dataset())
