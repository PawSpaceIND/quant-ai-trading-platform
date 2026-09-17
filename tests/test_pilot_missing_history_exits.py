"""Missing-data exits through the real local paper ledger; no running service or network."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from test_pilot_closure import publish_tick
from test_pilot_required_risk_gates import NOW, daily, runner
from test_portfolio_risk_controls import buy, plan

from quant_ai.agents.swarm import AgentAnalysisRequest
from quant_ai.domain.models import AssetClass, Market, OrderIntent, Side


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    for name in ("PRAMANA_ORDER_IDENTITY_MODE", "PRAMANA_OMS_DB", "PRAMANA_SECTOR_MAP_JSON",
                 "PRAMANA_SECTOR_MAP_FILE", "PRAMANA_OVERNIGHT_GROSS_CAP", "PRAMANA_OVERNIGHT_GAP_MONITOR"):
        monkeypatch.delenv(name, raising=False)
    actual = runner(tmp_path, daily())
    actual.daemon.clock = lambda: NOW
    publish_tick(actual, "100", NOW)
    broker = actual.daemon.tracker.broker
    broker.buy(OrderIntent("INFY", Market.INDIA, Side.BUY, 5, Decimal(100),
                           "synthetic-exit-fixture", tenant_id="pilot", stop_price=Decimal(95)))
    try:
        yield actual
    finally:
        broker._connection.close()


def lose_input(actual, missing):
    risk = actual.daemon.scheduler.pipeline.runtime.warden.book_risk
    if missing == "history":
        risk.history_provider = None
    elif missing == "map":
        risk.sector_map.clear()
    else:
        risk.sector_map.pop("TCS")


def execute(actual, side, quantity, now=NOW):
    runtime = actual.daemon.scheduler.pipeline.runtime
    proposal = replace(buy("INFY", quantity), decision_id="synthetic-" + side.value,
                       market=Market.INDIA, country="INDIA", side=side,
                       stop_price=Decimal(95) if side == Side.BUY else None,
                       take_profit_price=None)
    request = AgentAnalysisRequest("INFY", Market.INDIA, AssetClass.EQUITY, now, {}, 0)
    # Only consensus is supplied. The actual snapshot, pre-submit, Warden, broker,
    # cost ledger and canonical fill-evidence path still execute.
    return runtime._execute_proposal(request, (), proposal, plan(),
                                     runtime.snapshot_provider(), None, "pilot")


@pytest.mark.parametrize("missing", ["history", "map", "one_symbol"])
def test_missing_required_input_blocks_new_risk_but_covered_exit_fills_and_reconciles(seeded, missing):
    actual = seeded
    broker = actual.daemon.tracker.broker
    lose_input(actual, missing)
    denied = execute(actual, Side.BUY, 1)
    assert denied.fill is None and not denied.risk_decision.approved
    # Runtime identity notices the removed input before the Warden is reached.
    # Require that stronger first refusal, then verify the book gate independently.
    assert denied.risk_decision.reason == "pilot_strategy_manifest_unverified"
    assert actual.daemon.kill_switch.engaged
    runtime = actual.daemon.scheduler.pipeline.runtime
    adding = replace(buy("INFY", 1), market=Market.INDIA, country="INDIA")
    direct = runtime.warden.evaluate(adding, plan(), runtime.snapshot_provider(),
                                     tenant_id="pilot", now=NOW)
    assert not direct.approved and "book_risk_measure_unavailable" in direct.reason
    assert len(broker.ledger_entries("pilot")) == 1
    exited = execute(actual, Side.SELL, 5)
    assert exited.risk_decision.approved, exited.risk_decision.reason
    assert exited.fill is not None and exited.fill.filled_quantity == 5
    assert not broker.get_positions("pilot")
    assert len(broker.ledger_entries("pilot")) == 2
    assert broker._connection.execute("SELECT count(*) FROM paper_decision_evidence WHERE order_id=?",
                                      (exited.fill.order_id,)).fetchone()[0] == 1
    assert broker._connection.execute("SELECT count(*) FROM paper_cost_ledger WHERE order_id=?",
                                      (exited.fill.order_id,)).fetchone()[0] > 0
    assert broker.reconcile("pilot")["status"] == "matched"


@pytest.mark.parametrize("missing", ["history", "map", "one_symbol"])
def test_missing_inputs_never_make_stale_exit_prices_acceptable(seeded, missing):
    actual = seeded
    lose_input(actual, missing)
    later = NOW + timedelta(minutes=3)
    actual.daemon.clock = lambda: later
    result = execute(actual, Side.SELL, 5, later)
    assert result.fill is None and not result.risk_decision.approved
    assert "stale" in result.risk_decision.reason.lower()
    broker = actual.daemon.tracker.broker
    assert broker.get_positions("pilot")[0].quantity == 5
    assert len(broker.ledger_entries("pilot")) == 1
    assert broker.reconcile("pilot")["status"] == "matched"


def test_protective_exit_does_not_need_history_and_keeps_canonical_evidence(seeded):
    actual = seeded
    lose_input(actual, "history")
    publish_tick(actual, "90", NOW)
    actual.daemon.protection_tick(NOW)
    broker = actual.daemon.tracker.broker
    assert not broker.get_positions("pilot")
    assert len(broker.ledger_entries("pilot")) == 2
    assert broker._connection.execute("SELECT count(*) FROM paper_protection_evidence WHERE tenant_id='pilot'").fetchone()[0] == 1
    assert broker.reconcile("pilot")["status"] == "matched"
