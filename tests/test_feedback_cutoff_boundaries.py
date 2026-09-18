"""Positive clock-boundary checks over disposable journal data, not market evidence."""
from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from test_attribution_memory import SESSION, TENANT, broker_for
from test_specialist_feedback import add

from quant_ai.agents.contracts import AgentDomain, AgentEvidence, Stance
from quant_ai.analytics import feedback
from quant_ai.analytics.attribution import AgentAttributionEngine, restore_from_journal

D = Decimal
CUTOFF = SESSION + timedelta(hours=1)


@pytest.fixture
def ledger(tmp_path):
    value = broker_for(tmp_path)
    try:
        yield value
    finally:
        value.close()


def evidence(at):
    return (AgentEvidence(
        "supporter", AgentDomain.TECHNICAL, "INFY", Stance.BUY,
        D("0.5"), D("0.01"), D("0.005"), (), at, 0,
    ),)


@pytest.mark.parametrize("seconds,expected", [(-1, "0.625"), (0, "0.625"), (1, "0.5")])
def test_exit_boundary_uses_the_analysis_instant_inclusively(ledger, seconds, expected):
    add(ledger, exit_at=(CUTOFF + timedelta(seconds=seconds)).isoformat())
    engine = AgentAttributionEngine()
    restore_from_journal(engine, ledger, tenant_id=TENANT)
    result = engine.weight_evidence(evidence(CUTOFF), now=CUTOFF)
    assert result[0].confidence == D(expected)
    assert engine.feedback["checked_at"] == CUTOFF.isoformat()
    assert engine.feedback["status"] != "refused"


def test_later_explicit_evaluation_does_not_widen_a_frozen_binding(ledger):
    add(ledger, exit_at=(CUTOFF + timedelta(seconds=1)).isoformat())
    engine = AgentAttributionEngine()
    assert restore_from_journal(engine, ledger, tenant_id=TENANT, now=CUTOFF) == 0
    later = CUTOFF + timedelta(days=1)
    assert engine.weight_evidence(evidence(later), now=later)[0].confidence == D("0.5")
    engine.record(("unrelated",), D("999999"))
    assert engine.attribution() == ()
    assert engine.feedback["checked_at"] == CUTOFF.isoformat()


def test_earlier_evaluation_takes_precedence_over_a_later_binding_limit(ledger):
    add(ledger, exit_at=(CUTOFF + timedelta(seconds=1)).isoformat())
    engine = AgentAttributionEngine()
    assert restore_from_journal(engine, ledger, tenant_id=TENANT, now=CUTOFF + timedelta(days=1)) == 1
    assert engine.weight_evidence(evidence(CUTOFF), now=CUTOFF)[0].confidence == D("0.5")
    assert engine.attribution() == ()


def test_unfrozen_live_binding_advances_then_can_be_read_at_an_earlier_cutoff(ledger):
    add(ledger, exit_at=CUTOFF.isoformat())
    engine = AgentAttributionEngine()
    restore_from_journal(engine, ledger, tenant_id=TENANT)
    early, late = CUTOFF - timedelta(seconds=1), CUTOFF + timedelta(seconds=1)
    assert engine.weight_evidence(evidence(early), now=early)[0].confidence == D("0.5")
    assert engine.weight_evidence(evidence(late), now=late)[0].confidence == D("0.625")
    assert engine.attribution()[0].observations == 1
    assert engine.weight_evidence(evidence(early), now=early)[0].confidence == D("0.5")
    assert engine.attribution() == ()
    assert ledger._connection.execute(f"SELECT COUNT(*) FROM {feedback.TABLE}").fetchone()[0] == 1


def test_equivalent_ist_and_utc_cutoffs_select_the_same_projection(ledger):
    add(ledger, exit_at=CUTOFF.isoformat())
    engine = AgentAttributionEngine()
    restore_from_journal(engine, ledger, tenant_id=TENANT)
    utc = engine.weight_evidence(evidence(CUTOFF), now=CUTOFF)
    basis = engine.feedback["basis_sha256"]
    ist = CUTOFF.astimezone(ZoneInfo("Asia/Kolkata"))
    converted = engine.weight_evidence(evidence(ist), now=ist)
    assert utc[0].confidence == converted[0].confidence == D("0.625")
    assert engine.feedback["basis_sha256"] == basis
    assert engine.feedback["checked_at"] == CUTOFF.isoformat()


def test_naive_evaluation_time_refuses_instead_of_reusing_previous_weights(ledger):
    add(ledger, exit_at=CUTOFF.isoformat())
    engine = AgentAttributionEngine()
    assert restore_from_journal(engine, ledger, tenant_id=TENANT) == 1
    assert engine.weight_evidence(evidence(CUTOFF), now=CUTOFF.replace(tzinfo=None))[0].confidence == D("0.5")
    assert engine.feedback["status"] == "refused"
    assert engine.attribution() == ()


def test_report_observation_clock_is_distinct_from_the_older_cadence_cutoff(tmp_path):
    import json

    from test_learning_monitor import NOW, setup_monitor
    from test_pilot_closure import runner_for

    path, _, _ = setup_monitor(tmp_path)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    daemon = runner_for(runtime).daemon
    daemon.clock = lambda: NOW
    daemon.decision_quality_report_path = path.parent / "decision-quality.json"
    cadence = NOW - timedelta(minutes=10)
    try:
        daemon._write_decision_quality(cadence)
        report = json.loads(daemon.decision_quality_report_path.read_text())
        assert report["window"]["until"] == cadence.isoformat()
        assert report["learning_feedback"]["checked_at"] == NOW.isoformat()
        assert report["probability_drift"]["status"] == "healthy"
        assert daemon.tracker.broker.ledger_entries(daemon.tenant_id) == ()
    finally:
        daemon.tracker.broker.close()
