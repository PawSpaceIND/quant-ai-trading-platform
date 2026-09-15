import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest
from test_pilot_closure import INSTRUMENT, publish_tick, runner_for

from quant_ai.domain.models import AssetClass, Market, OrderIntent, Side
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.execution.protective_exits import ProtectiveExitEngine
from quant_ai.execution.risk_state import SQLiteRiskStateStore


def buy(**changes):
    return replace(OrderIntent("INFY", Market.INDIA, Side.BUY, 1, D(100), "test",
                               tenant_id="pilot", stop_price=D(95)), **changes)


@pytest.mark.parametrize("changes,reason", [
    ({"stop_price": None}, "valid_stop_required"),
    ({"stop_price": D("NaN")}, "valid_stop_required"),
    ({"stop_price": D("sNaN")}, "valid_stop_required"),
    ({"stop_price": D("Infinity")}, "valid_stop_required"),
    ({"stop_price": D(0)}, "valid_stop_required"),
    ({"stop_price": D(-1)}, "valid_stop_required"),
    ({"stop_price": D(100)}, "below_entry"),
    ({"stop_price": D(101)}, "below_entry"),
    ({"take_profit_price": D(100)}, "above_entry"),
    ({"take_profit_price": D(99)}, "above_entry"),
    ({"take_profit_price": D("NaN")}, "above_entry"),
    ({"take_profit_price": D("Infinity")}, "above_entry"),
    ({"reference_price": D("NaN")}, "positive quantity"),
    ({"reference_price": D("Infinity")}, "positive quantity"),
    ({"quantity": True}, "positive quantity"),
    ({"quantity": 1.5}, "positive quantity"),
])
def test_invalid_pilot_entry_rolls_back_account_fill_costs_proof_and_retry_key(tmp_path, changes, reason):
    broker = PaperBrokerService(tmp_path / "ledger.db")
    broker.configure_pilot((INSTRUMENT,), "pilot")
    before = tuple(broker._connection.iterdump())
    evidence = {"schema": "pramana.swarm_fill.v1", "event_type": "swarm_fill"}
    with pytest.raises(ValueError, match=reason):
        broker.submit_with_evidence(buy(**changes), evidence, "retryable")
    assert tuple(broker._connection.iterdump()) == before
    assert broker.submit_with_evidence(buy(), evidence, "retryable").status == "FILLED"


def test_actual_friction_price_and_inherited_target_must_allow_new_entry(tmp_path):
    broker = PaperBrokerService(tmp_path / "ledger.db", slippage_bps=D(100))
    broker.configure_pilot((INSTRUMENT,), "pilot")
    with pytest.raises(ValueError, match="above_entry"):
        broker.buy(buy(take_profit_price=D("100.5")))  # Reference is 100; fill would be 101.
    broker.buy(buy(take_profit_price=D(110)))
    before = tuple(broker._connection.iterdump())
    for intent, reason in [
        (buy(reference_price=D(109)), "above_entry"),  # Inherited target is crossed by friction.
        (buy(stop_price=D(90)), "cannot_loosen"),
        (buy(stop_price=None), "valid_stop_required"),  # Scaling must explicitly supply a stop.
    ]:
        with pytest.raises(ValueError, match=reason):
            broker.buy(intent)
        assert tuple(broker._connection.iterdump()) == before
    broker.buy(buy(stop_price=D(96)))
    assert broker.get_positions("pilot")[0].stop_price == 96
    assert broker.reconcile("pilot")["status"] == "matched"


def test_legacy_missing_stop_reconciles_but_blocks_new_risk_and_latches_restart_halt(tmp_path):
    broker = PaperBrokerService(tmp_path / "ledger.db")
    broker.buy(buy(stop_price=None))  # Legacy, before pilot configuration.
    broker.close()
    runner = runner_for(tmp_path)
    broker = runner.daemon.tracker.broker
    assert broker.reconcile("pilot")["status"] == "matched"
    report = broker.protection_coverage("pilot")
    assert report["status"] == "incomplete" and report["missingStopCount"] == 1
    assert runner.daemon.kill_switch.reason == "paper_position_protection_incomplete"
    before = tuple(broker._connection.iterdump())
    with pytest.raises(ValueError, match="pilot_halted"):
        broker.buy(buy())
    assert tuple(broker._connection.iterdump()) == before
    # Covered sale is still allowed. Fault halt is not silently cleared by becoming flat.
    broker.sell(buy(side=Side.SELL, stop_price=None))
    runner.daemon.protection_tick()
    assert runner.daemon.protection_coverage["status"] == "complete"
    assert runner.daemon.kill_switch.engaged
    broker.close()
    restarted = runner_for(tmp_path)
    assert restarted.daemon.kill_switch.engaged
    assert restarted.daemon.kill_switch.reason == "paper_position_protection_incomplete"


def test_direct_boundary_checks_whole_book_and_halt_across_connections(tmp_path):
    broker = PaperBrokerService(tmp_path / "ledger.db")
    broker.buy(buy(stop_price=None))
    broker.configure_pilot((INSTRUMENT,), "pilot")
    with pytest.raises(ValueError, match="pilot_protection_incomplete"):
        broker.buy(buy())
    broker.sell(buy(side=Side.SELL))
    risk = SQLiteRiskStateStore(tmp_path / "ledger.db")
    risk.set_kill_switch("other", True, "other tenant")
    broker.buy(buy())
    risk.set_kill_switch("pilot", True, "independent risk halt")
    before = tuple(broker._connection.iterdump())
    with pytest.raises(ValueError, match="pilot_halted"):
        broker.buy(buy())
    assert tuple(broker._connection.iterdump()) == before
    assert broker.sell(buy(side=Side.SELL)).status == "FILLED"
    risk.close()


def test_pilot_scope_binds_exact_asset_class_after_restart_and_accepts_iterators(tmp_path):
    broker = PaperBrokerService(tmp_path / "ledger.db")
    broker.configure_pilot(iter((INSTRUMENT,)), "pilot")
    broker.close()
    broker = PaperBrokerService(tmp_path / "ledger.db")
    before = tuple(broker._connection.iterdump())
    with pytest.raises(ValueError, match="pilot_order_out_of_scope"):
        broker.buy(buy(asset_class=AssetClass.ETF))
    assert tuple(broker._connection.iterdump()) == before
    assert broker.buy(buy()).status == "FILLED"
    with pytest.raises(ValueError, match="pilot_existing_positions_out_of_scope"):
        broker.configure_pilot((replace(INSTRUMENT, asset_class=AssetClass.ETF),), "pilot")


def test_scope_validation_and_fill_exclude_concurrent_configuration_writes(tmp_path, monkeypatch):
    database = tmp_path / "ledger.db"
    broker = PaperBrokerService(database)
    broker.configure_pilot((INSTRUMENT,), "pilot")
    other = sqlite3.connect(database, timeout=0)
    original = broker._assert_pilot_order
    def check(order):
        # A second writer must be excluded already, before scope is read. It can
        # acquire the lock again after the full validation/fill transaction completes.
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            other.execute("BEGIN IMMEDIATE")
        return original(order)
    monkeypatch.setattr(broker, "_assert_pilot_order", check)
    assert broker.buy(buy()).status == "FILLED"
    other.execute("BEGIN IMMEDIATE")
    other.rollback()
    other.close()


def test_legacy_symbol_only_scope_needs_explicit_configuration_for_new_risk(tmp_path):
    broker = PaperBrokerService(tmp_path / "ledger.db")
    broker.configure_pilot((INSTRUMENT,), "pilot")
    broker.buy(buy())
    with broker._connection:
        broker._connection.execute("UPDATE pilot_scope SET symbols='[\"INFY\"]'")
    with pytest.raises(ValueError, match="pilot_scope_requires_instrument_configuration"):
        broker.buy(buy())
    assert broker.sell(buy(side=Side.SELL)).status == "FILLED"
    broker.configure_pilot((INSTRUMENT,), "pilot")
    assert broker.buy(buy()).status == "FILLED"


@pytest.mark.parametrize("raw", ["NaN", "sNaN", "Infinity", "-1", "0", "broken", "1e999"])
def test_corrupt_stop_is_reported_without_repair_and_valid_other_exit_still_fills(tmp_path, raw):
    broker = PaperBrokerService(tmp_path / "ledger.db")
    broker.buy(buy())
    broker.buy(buy(symbol="TCS"))
    with broker._connection:
        broker._connection.execute("UPDATE paper_positions SET stop_price=? WHERE symbol='INFY'", (raw,))
    before = tuple(broker._connection.iterdump())
    report = broker.protection_coverage("pilot")
    assert report["status"] == "invalid" and report["coveredCount"] == 1
    assert report["issues"] == [{"code": "invalid_stop", "key": "INDIA:EQUITY:INFY"}]
    assert tuple(broker._connection.iterdump()) == before
    exits = ProtectiveExitEngine(broker, lambda _: D(90), tenant_id="pilot").evaluate()
    assert len(exits) == 1 and exits[0].symbol == "TCS" and exits[0].filled
    assert broker._connection.execute("SELECT stop_price FROM paper_positions WHERE symbol='INFY'").fetchone()[0] == raw


def test_valid_target_survives_invalid_stop_and_invalid_mark_never_fills(tmp_path):
    broker = PaperBrokerService(tmp_path / "ledger.db")
    broker.buy(buy(take_profit_price=D(110)))
    with broker._connection:
        broker._connection.execute("UPDATE paper_positions SET stop_price='NaN'")
    engine = ProtectiveExitEngine(broker, lambda _: D("NaN"), tenant_id="pilot")
    assert engine.evaluate() == ()
    engine.mark_resolver = lambda _: D(115)
    assert engine.evaluate()[0].filled
    assert broker.reconcile("pilot")["status"] == "matched"


def test_coverage_is_tenant_scoped_bounded_and_does_not_reject_profit_stops(tmp_path):
    broker = PaperBrokerService(tmp_path / "ledger.db")
    broker.buy(buy())
    broker.buy(buy(tenant_id="other", stop_price=None))
    with broker._connection:
        broker._connection.execute("UPDATE paper_positions SET stop_price='105',take_profit_price='110' WHERE tenant_id='pilot'")
    report = broker.protection_coverage("pilot")
    assert report["status"] == "complete"  # Held profit stop need not be below original entry.
    assert report["positionCount"] == report["coveredCount"] == 1
    assert report["ledgerId"] == 1
    with broker._connection:
        broker._connection.execute("UPDATE paper_positions SET take_profit_price='104' WHERE tenant_id='pilot'")
    assert broker.protection_coverage("pilot")["issues"][0]["code"] == "target_not_above_stop"
    for i in range(60):
        broker.buy(buy(symbol=f"S{i}", tenant_id="many", stop_price=None))
    report = broker.protection_coverage("many")
    assert report["issueCount"] == 60 and len(report["issues"]) == 50


@pytest.mark.parametrize("error", [ValueError("temporary execution failure"), RuntimeError("unexpected execution failure")])
def test_failed_protective_exit_durably_halts_entries_and_can_retry(tmp_path, monkeypatch, error):
    runner = runner_for(tmp_path)
    daemon, now = runner.daemon, datetime.now(timezone.utc)
    broker = daemon.tracker.broker
    broker.buy(buy())
    publish_tick(runner, "90", now)
    original = broker.sell_protected
    def fail(*args):
        raise error
    monkeypatch.setattr(broker, "sell_protected", fail)
    if isinstance(error, ValueError):
        daemon.protection_tick(now)
        assert not daemon.protective_exits[0].filled
    else:
        with pytest.raises(RuntimeError, match="unexpected"):
            daemon.protection_tick(now)
    assert daemon.kill_switch.reason == "protective_exit_failed"
    assert daemon.tracker.risk_state.kill_switch_state("pilot") == (True, "protective_exit_failed")
    assert broker.get_positions("pilot")
    monkeypatch.setattr(broker, "sell_protected", original)
    daemon.protection_tick(now)
    assert not broker.get_positions("pilot")
    assert daemon.kill_switch.engaged


def test_telemetry_rechecks_corrupt_levels_and_emits_valid_json_with_latched_halt(tmp_path):
    runner = runner_for(tmp_path)
    daemon, now = runner.daemon, datetime.now(timezone.utc)
    broker = daemon.tracker.broker
    broker.buy(buy())
    publish_tick(runner, "100", now)
    daemon.telemetry.publish(now)
    head = broker.protection_coverage("pilot")["ledgerId"]
    with broker._connection:
        broker._connection.execute("UPDATE paper_positions SET stop_price='broken',take_profit_price='sNaN'")
    assert daemon._pilot_pre_submit(buy()) == "pilot_protection_incomplete"
    daemon.protection_tick(now)
    state = json.loads(broker._connection.execute("SELECT payload FROM pilot_runtime").fetchone()[0])
    assert state["halted"] and state["protectionCoverage"]["status"] == "invalid"
    assert state["protectionCoverage"]["ledgerId"] == head
    assert state["protectionCoverage"]["issueCount"] == 2
    # Display-safe values do not erase or repair the invalid raw database records.
    values = json.loads(broker._connection.execute("SELECT payload FROM paper_live_valuations").fetchone()[0])
    assert values["holdings"][0]["stopPrice"] is None
    assert values["holdings"][0]["takeProfitPrice"] is None
    assert broker._connection.execute("SELECT stop_price FROM paper_positions").fetchone()[0] == "broken"
