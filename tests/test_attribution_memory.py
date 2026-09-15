"""Attribution is the one part of the engine that learns, so it must outlive a restart.

The daily Zerodha token renewal restarts the daemon every morning. Before the journal
rebuild these tests cover, every specialist's score reset with it, so the engine could
never carry a lesson from one session into the next.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from quant_ai.analytics import decision_journal as journal
from quant_ai.analytics.attribution import (
    MIN_REGIME_OBSERVATIONS,
    AgentAttributionEngine,
    restore_from_journal,
)
from quant_ai.execution.paper_ledger import PaperBrokerService

TENANT = "ghost"
SESSION = datetime(2026, 9, 15, 6, 0, tzinfo=timezone.utc)
AGENTS = {"technical": {"stance": "BUY", "confidence": "0.7"},
          "macro": {"stance": "BUY", "confidence": "0.6"}}


def broker_for(tmp_path) -> PaperBrokerService:
    return PaperBrokerService(database=str(tmp_path / "ledger.db"))


def closed_trade(broker, decision_id, *, pnl, regime, agents=None, minutes=0, tenant=TENANT):
    journal.insert_decision(broker, {
        "decision_id": decision_id, "tenant_id": tenant, "symbol": "INFY", "market": "INDIA",
        "asset_class": "EQUITY", "decided_at": (SESSION + timedelta(minutes=minutes)).isoformat(),
        "stance": "BUY", "side": "BUY", "confidence": "0.7", "expected_return": "0.01",
        "expected_risk": "0.005", "reference_price": "100", "stop_price": "95",
        "take_profit_price": "110", "regime": regime, "mode": "llm", "governance": "filled",
        "reason": None, "order_id": decision_id,
        "agents": json.dumps(AGENTS if agents is None else agents),
        "realized_net_pnl": str(pnl),
    })


def test_a_restart_no_longer_erases_what_the_specialists_earned(tmp_path):
    broker = broker_for(tmp_path)
    for index in range(4):
        closed_trade(broker, f"d{index}", pnl=100 if index < 3 else -50,
                     regime="trending_up", minutes=index * 10)

    before = AgentAttributionEngine()
    for index in range(4):
        before.record(("technical", "macro"), Decimal(100 if index < 3 else -50), "trending_up")

    # The morning restart: a brand new engine, rebuilt only from the durable journal.
    after = AgentAttributionEngine()
    assert after.weight_for("technical") == (Decimal(1), "unscored")
    assert restore_from_journal(after, broker, tenant_id=TENANT) == 4

    assert after.weight_for("technical") == before.weight_for("technical")
    restored = {row.agent_id: row for row in after.attribution()}
    assert restored["technical"].observations == 4
    assert restored["technical"].wins == 3
    assert restored["technical"].losses == 1
    assert restored["technical"].pnl == Decimal(125)  # (300 - 50) shared across two agents


def test_an_agent_is_scored_per_regime_once_the_regime_has_enough_evidence(tmp_path):
    broker = broker_for(tmp_path)
    # Right in a trend, wrong in a range, with enough of each to be believed.
    for index in range(MIN_REGIME_OBSERVATIONS):
        closed_trade(broker, f"up{index}", pnl=100, regime="trending_up", minutes=index)
    for index in range(MIN_REGIME_OBSERVATIONS):
        closed_trade(broker, f"rg{index}", pnl=-100, regime="ranging", minutes=100 + index)

    engine = AgentAttributionEngine()
    restore_from_journal(engine, broker, tenant_id=TENANT)

    trending, trending_source = engine.weight_for("technical", "trending_up")
    ranging, ranging_source = engine.weight_for("technical", "ranging")
    blended, blended_source = engine.weight_for("technical")

    assert trending_source == "trending_up" and ranging_source == "ranging"
    assert blended_source == "blended"
    assert trending > blended > ranging
    assert trending == Decimal("1.25") and ranging == Decimal("0.75")


def test_a_thin_regime_record_is_not_trusted_yet(tmp_path):
    broker = broker_for(tmp_path)
    for index in range(MIN_REGIME_OBSERVATIONS - 1):
        closed_trade(broker, f"d{index}", pnl=100, regime="high_volatility", minutes=index)

    engine = AgentAttributionEngine()
    restore_from_journal(engine, broker, tenant_id=TENANT)

    weight, source = engine.weight_for("technical", "high_volatility")
    assert source == "blended", "a handful of trades in one regime is a story, not evidence"
    assert weight == engine.weight_for("technical")[0]


def test_open_and_unresolved_decisions_are_not_scored(tmp_path):
    broker = broker_for(tmp_path)
    closed_trade(broker, "closed", pnl=100, regime="ranging")
    journal.insert_decision(broker, {
        "decision_id": "still-open", "tenant_id": TENANT, "symbol": "INFY", "market": "INDIA",
        "asset_class": "EQUITY", "decided_at": SESSION.isoformat(), "stance": "BUY", "side": "BUY",
        "confidence": "0.7", "expected_return": "0.01", "expected_risk": "0.005",
        "reference_price": "100", "stop_price": "95", "take_profit_price": "110",
        "regime": "ranging", "mode": "llm", "governance": "filled", "reason": None,
        "order_id": "o2", "agents": json.dumps(AGENTS), "realized_net_pnl": None,
    })

    engine = AgentAttributionEngine()
    assert restore_from_journal(engine, broker, tenant_id=TENANT) == 1
    assert engine.attribution()[0].observations == 1


def test_a_corrupt_row_costs_only_itself(tmp_path):
    broker = broker_for(tmp_path)
    closed_trade(broker, "good", pnl=100, regime="ranging")
    closed_trade(broker, "bad-agents", pnl=100, regime="ranging", agents={})
    journal.insert_decision(broker, {
        "decision_id": "bad-pnl", "tenant_id": TENANT, "symbol": "INFY", "market": "INDIA",
        "asset_class": "EQUITY", "decided_at": SESSION.isoformat(), "stance": "BUY", "side": "BUY",
        "confidence": "0.7", "expected_return": "0.01", "expected_risk": "0.005",
        "reference_price": "100", "stop_price": "95", "take_profit_price": "110",
        "regime": "ranging", "mode": "llm", "governance": "filled", "reason": None,
        "order_id": "o3", "agents": json.dumps(AGENTS), "realized_net_pnl": "not-a-number",
    })

    engine = AgentAttributionEngine()
    assert restore_from_journal(engine, broker, tenant_id=TENANT) == 1
    assert engine.attribution()[0].observations == 1


def test_an_unreadable_history_is_a_cold_start_not_a_failed_boot(tmp_path):
    class Broken:
        _lock = None

    engine = AgentAttributionEngine()
    assert restore_from_journal(engine, Broken(), tenant_id=TENANT) == 0
    assert engine.attribution() == ()


def test_the_weight_band_still_bounds_a_lucky_streak(tmp_path):
    broker = broker_for(tmp_path)
    for index in range(50):
        closed_trade(broker, f"win{index}", pnl=1000, regime="trending_up", minutes=index)

    engine = AgentAttributionEngine()
    restore_from_journal(engine, broker, tenant_id=TENANT)

    weight, _ = engine.weight_for("technical", "trending_up")
    assert weight == Decimal("1.25"), "no run of luck may hand one agent the book"


def test_the_ghost_daemon_boots_with_yesterdays_scores(tmp_path):
    """The real factory, not a hand-built engine: a restart recovers what was learned."""
    from test_pilot_closure import runner_for

    first = runner_for(tmp_path)
    broker = first.daemon.tracker.broker
    tenant = first.daemon.tenant_id
    for index in range(4):
        closed_trade(broker, f"d{index}", pnl=100 if index < 3 else -50,
                     regime="trending_up", minutes=index * 10, tenant=tenant)
    assert first.daemon.scheduler.pipeline.runtime.attribution.attribution() == ()

    # Same ledger directory, a new daemon: exactly what the morning restart does.
    second = runner_for(tmp_path)
    recovered = {row.agent_id: row for row in
                 second.daemon.scheduler.pipeline.runtime.attribution.attribution()}

    assert set(recovered) == {"technical", "macro"}
    assert recovered["technical"].observations == 4
    assert recovered["technical"].hit_rate == Decimal(3) / Decimal(4)
