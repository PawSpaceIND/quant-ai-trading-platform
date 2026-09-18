"""Executable synthetic-journal feedback tests; no provider or trading requests."""
from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from decimal import Decimal

import pytest
from test_attribution_memory import SESSION, TENANT, broker_for, closed_trade

from quant_ai.agents.contracts import AgentDomain, AgentEvidence, Stance
from quant_ai.analytics import decision_journal as journal
from quant_ai.analytics import feedback
from quant_ai.analytics.attribution import AgentAttributionEngine, restore_from_journal

D = Decimal
NOW = SESSION + timedelta(days=2)
VOTES = {
    "supporter": {"stance": "BUY", "confidence": "0.7"},
    "dissenter": {"stance": "SELL", "confidence": "0.8"},
    "neutral": {"stance": "NEUTRAL", "confidence": "0.6"},
    "muted": {"stance": "BUY", "confidence": "0"},
}


@pytest.fixture
def broker(tmp_path):
    value = broker_for(tmp_path)
    try:
        yield value
    finally:
        value.close()


def add(broker, name="entry", pnl="100", **changes):
    closed_trade(broker, name, pnl=pnl, regime="trending_up", agents=VOTES)
    if changes:
        names = list(changes)
        with broker._connection as db:
            db.execute(
                "UPDATE paper_decision_journal SET " + ",".join(f"{key}=?" for key in names)
                + " WHERE decision_id=?", (*[changes[key] for key in names], name),
            )


def restore(engine, broker):
    return restore_from_journal(engine, broker, tenant_id=TENANT, now=NOW)


@pytest.mark.parametrize("pnl,weight", [("100", "1.25"), ("-100", "0.75")])
def test_only_supporters_receive_realized_entry_credit(broker, pnl, weight):
    add(broker, pnl=pnl)
    engine = AgentAttributionEngine()
    assert restore(engine, broker) == 1
    assert engine.weight_for("supporter")[0] == D(weight)
    for name in ("dissenter", "neutral", "muted"):
        assert engine.weight_for(name) == (D(1), "unscored")
    assert engine.attribution()[0].pnl == D(pnl)
    event = engine.feedback["events"][0]
    assert event["credited_agents"] == ["supporter"]
    assert event["uncredited_agents"] == ["dissenter", "muted", "neutral"]
    assert event["replay_before"] == {"supporter": "1"}
    assert event["replay_after"] == {"supporter": weight}


def test_restart_and_repeated_refresh_do_not_duplicate_outcomes(broker):
    add(broker)
    engine = AgentAttributionEngine()
    for _ in range(3):
        assert restore(engine, broker) == 1
        assert engine.attribution()[0].observations == 1
    cold = AgentAttributionEngine()
    assert restore(cold, broker) == 1
    assert cold.attribution() == engine.attribution()
    assert cold.feedback["events"] == engine.feedback["events"]
    assert broker._connection.execute(f"SELECT COUNT(*) FROM {feedback.TABLE}").fetchone()[0] == 1


def test_bound_legacy_delta_callback_cannot_credit_the_latest_unrelated_trace(broker):
    add(broker)
    engine = AgentAttributionEngine()
    restore(engine, broker)
    engine.record(("unrelated-trace", "dissenter"), D("999999"), "ranging")
    assert engine.weight_for("unrelated-trace") == (D(1), "unscored")
    assert engine.weight_for("dissenter") == (D(1), "unscored")
    assert engine.attribution()[0].pnl == D(100)
    assert engine.attribution()[0].observations == 1


def test_new_resolved_outcome_is_used_before_next_evidence_weighting(broker):
    engine = AgentAttributionEngine()
    assert restore(engine, broker) == 0
    add(broker)
    evidence = AgentEvidence("supporter", AgentDomain.TECHNICAL, "INFY", Stance.BUY,
                             D("0.5"), D("0.01"), D("0.005"), (), SESSION, 0)
    weighted = engine.weight_evidence((evidence,), "trending_up")[0]
    assert weighted.confidence == D("0.625")
    assert "attribution_weight=1.25:blended" in weighted.rationale
    assert engine.feedback["credited_entries"] == 1


@pytest.mark.parametrize("change", [
    {"realized_net_pnl": None}, {"realized_net_pnl": "NaN"},
    {"realized_net_pnl": "Infinity"}, {"exit_at": None},
    {"exit_at": (SESSION - timedelta(minutes=1)).isoformat()},
    {"exit_at": (NOW + timedelta(seconds=1)).isoformat()},
    {"exit_at": "2026-09-15T07:00:00"}, {"order_id": None},
    {"governance": "abstained"}, {"side": "SELL"}, {"stance": "NEUTRAL"},
    {"agents": "[]"}, {"agents": "{}"}, {"agents": "not-json"},
    {"agents": '{"supporter":{"stance":"BUY","confidence":"NaN"}}'},
    {"agents": '{"supporter":{"stance":"BUY","confidence":"-0.1"}}'},
    {"agents": '{"supporter":{"stance":"BUY","confidence":"1.1"}}'},
    {"agents": '{"supporter":{"stance":"BUY"}}'},
    {"agents": '{"supporter":{"stance":"BUY","confidence":"0"}}'},
    {"agents": '{"supporter":{"stance":"NEUTRAL","confidence":"0.8"}}'},
    {"agents": '{"supporter":{"stance":"BUY","confidence":"0.5"},"supporter":{"stance":"SELL","confidence":"0.8"}}'},
])
def test_invalid_or_ineligible_outcomes_never_change_weights(broker, change):
    add(broker, **change)
    engine = AgentAttributionEngine()
    assert restore(engine, broker) == 0
    assert engine.attribution() == ()
    assert broker._connection.execute(f"SELECT COUNT(*) FROM {feedback.TABLE}").fetchone()[0] == 0


def test_source_mutation_after_consumption_is_refused_without_recredit(broker):
    add(broker)
    engine = AgentAttributionEngine()
    assert restore(engine, broker) == 1
    journal.update_decision(broker, "entry", realized_net_pnl="200")
    assert restore(engine, broker) == 0
    assert engine.feedback["status"] == "refused"
    assert engine.attribution() == ()
    raw = broker._connection.execute(f"SELECT payload FROM {feedback.TABLE}").fetchone()[0]
    assert json.loads(raw)["net_pnl"] == "100"


@pytest.mark.parametrize("verb", ["UPDATE", "DELETE"])
def test_feedback_audit_is_append_only(broker, verb):
    add(broker)
    restore(AgentAttributionEngine(), broker)
    sql = f"UPDATE {feedback.TABLE} SET sha256='changed'" if verb == "UPDATE" else f"DELETE FROM {feedback.TABLE}"
    with pytest.raises(sqlite3.IntegrityError, match="append-only"), broker._connection as db:
        db.execute(sql)


def test_corrupt_audit_digest_refuses_even_when_source_payload_is_unchanged(broker):
    add(broker)
    engine = AgentAttributionEngine()
    restore(engine, broker)
    with broker._connection as db:
        db.execute(f"DROP TRIGGER {feedback.TABLE}_update_blocked")
        db.execute(f"UPDATE {feedback.TABLE} SET sha256='corrupt'")
    assert restore(engine, broker) == 0
    assert engine.feedback["status"] == "refused"
    assert engine.attribution() == ()


def test_deleted_consumed_source_does_not_silently_reset_history(broker):
    add(broker)
    engine = AgentAttributionEngine()
    restore(engine, broker)
    with broker._connection as db:
        db.execute("DELETE FROM paper_decision_journal")
    assert restore(engine, broker) == 0
    assert engine.feedback["status"] == "refused"


def test_duplicate_order_identity_cannot_double_credit_two_decisions(broker):
    add(broker, "first")
    add(broker, "second", order_id="first")
    engine = AgentAttributionEngine()
    assert restore(engine, broker) == 0
    assert engine.attribution() == ()
    assert engine.feedback["status"] == "refused"


def test_tenant_scoping_and_since_bound_are_retained(broker):
    add(broker)
    closed_trade(broker, "other", pnl=-1000, regime="ranging", agents=VOTES, tenant="other")
    engine = AgentAttributionEngine()
    assert restore(engine, broker) == 1
    assert engine.attribution()[0].pnl == D(100)
    assert restore_from_journal(engine, broker, tenant_id="other", now=NOW) == 1
    assert engine.attribution()[0].pnl == D(-1000)
    assert restore_from_journal(engine, broker, tenant_id=TENANT, now=NOW, since=SESSION + timedelta(hours=1)) == 0
    assert engine.attribution() == ()


def test_refresh_does_not_commit_an_unrelated_open_transaction(broker):
    add(broker)
    db = broker._connection
    db.execute("UPDATE paper_decision_journal SET realized_net_pnl='300'")
    assert db.in_transaction
    engine = AgentAttributionEngine()
    assert restore(engine, broker) == 0
    assert db.in_transaction
    db.rollback()
    assert journal.load_rows(broker, tenant_id=TENANT)[0]["realized_net_pnl"] == "100"


def test_original_journal_bytes_and_trading_tables_are_not_rewritten(broker):
    add(broker)
    before = journal.load_rows(broker, tenant_id=TENANT)
    engine = AgentAttributionEngine()
    restore(engine, broker)
    assert journal.load_rows(broker, tenant_id=TENANT) == before
    assert broker.ledger_entries(TENANT) == ()


def test_actual_factory_binds_feedback_not_the_unrelated_trace(tmp_path):
    from test_pilot_closure import runner_for

    runner = runner_for(tmp_path)
    broker = runner.daemon.tracker.broker
    engine = runner.daemon.scheduler.pipeline.runtime.attribution
    try:
        closed_trade(broker, "factory-entry", pnl=100, regime="trending_up", agents=VOTES,
                     tenant=runner.daemon.tenant_id)
        engine.record(("unrelated-trace",), D(9000))
        assert engine.weight_for("supporter")[0] == D("1.25")
        assert engine.weight_for("dissenter") == (D(1), "unscored")
        assert engine.weight_for("unrelated-trace") == (D(1), "unscored")
        assert engine.feedback["events"][0]["decision_id"] == "factory-entry"
    finally:
        broker.close()


def test_explicit_restore_cutoff_survives_later_weighting(broker):
    add(broker, exit_at=(SESSION + timedelta(hours=1)).isoformat())
    engine = AgentAttributionEngine()
    cutoff = SESSION + timedelta(minutes=5)
    assert restore_from_journal(engine, broker, tenant_id=TENANT, now=cutoff) == 0
    evidence = AgentEvidence("supporter", AgentDomain.TECHNICAL, "INFY", Stance.BUY,
                             D("0.5"), D("0.01"), D("0.005"), (), cutoff, 0)
    assert engine.weight_evidence((evidence,))[0].confidence == D("0.5")
    assert engine.attribution() == ()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_swarm_uses_the_analysis_cutoff_for_feedback(broker, monkeypatch, asynchronous):
    import asyncio

    from quant_ai.agents.swarm import AgentAnalysisRequest
    from quant_ai.agents.swarm_runtime import SwarmPaperTradingService
    from quant_ai.domain.models import AssetClass, Market

    add(broker, exit_at=(SESSION + timedelta(hours=1)).isoformat())
    engine = AgentAttributionEngine()
    restore_from_journal(engine, broker, tenant_id=TENANT)
    service = SwarmPaperTradingService(broker=broker, attribution=engine)
    cutoff = SESSION + timedelta(minutes=5)
    evidence = AgentEvidence("supporter", AgentDomain.TECHNICAL, "INFY", Stance.BUY,
                             D("0.5"), D("0.01"), D("0.005"), (), cutoff, 0)
    monkeypatch.setattr(service.cio, "propose", lambda *args, **kwargs: None)
    async def proposal(*args, **kwargs):
        return None
    monkeypatch.setattr(service.cio, "propose_async", proposal)
    monkeypatch.setattr(service, "_execute_proposal", lambda request, weighted, *args: weighted[0].confidence)
    args = (AgentAnalysisRequest("INFY", Market.INDIA, AssetClass.EQUITY, cutoff, {}), (evidence,), None, None)
    kwargs = {"quantity": 1, "reference_price": D(100), "stop_price": D(95),
              "take_profit_price": D(110), "country": "INDIA"}
    result = asyncio.run(service.execute_async(*args, **kwargs)) if asynchronous else service.execute(*args, **kwargs)
    assert result == D("0.5")
    assert engine.attribution() == ()
