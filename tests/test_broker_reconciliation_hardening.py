"""Adversarial regressions; all broker/account evidence here is synthetic."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest
from test_broker_lifecycle_reconciliation import (
    NOW,
    D,
    OrderTransport,
    account_snapshot,
    broker_order,
    broker_trade,
    expected_account,
    oms_order,
    order_capture,
    uncertain_order,
)

from quant_ai.domain.models import Market
from quant_ai.execution.broker_lifecycle import (
    OmsBrokerLifecycleReconciler,
    reconcile_external_account,
)
from quant_ai.execution.broker_observation import digest
from quant_ai.orders.oms import DurableOms
from quant_ai.orders.state import OrderState


def reconcile(oms, capture, bindings=None):
    return OmsBrokerLifecycleReconciler().reconcile(
        oms, capture, tenant_id="tenant", account_ref=capture["accountRef"],
        bindings=bindings,
    now=NOW)


def resign(payload):
    payload["sha256"] = digest({k: v for k, v in payload.items() if k != "sha256"})
    return payload


def test_equal_filled_quantity_does_not_hide_wrong_callback_price(tmp_path):
    capture = order_capture(OrderTransport(
        [broker_order(status="OPEN", filled_quantity=1, pending_quantity=2, average_price=0)],
        [broker_trade()],
    ))
    with DurableOms(tmp_path / "price.sqlite") as oms:
        row = uncertain_order(oms)
        oms.submitted(row.client_order_id, broker_order_id="order-1", now=NOW)
        oms.fill(row.client_order_id, fill_id="callback-1", quantity=1,
                 price=D(99), broker_order_id="order-1", now=NOW)
        before = tuple(oms.db.iterdump())
        result = reconcile(oms, capture)
        assert result.status == "discrepancy"
        assert "broker_fill_price_mismatch" in {i.code for i in result.issues}
        assert tuple(oms.db.iterdump()) == before


def test_closed_orders_are_checked_again_without_manual_bindings(tmp_path):
    with DurableOms(tmp_path / "closed.sqlite") as oms:
        row = uncertain_order(oms)
        assert reconcile(oms, order_capture(), {row.client_order_id: "order-1"}).status == "matched"
        restated = order_capture(OrderTransport(
            [broker_order(average_price=120)],
            [broker_trade(average_price=118),
             broker_trade(trade_id="trade-2", quantity=2, average_price=121)],
        ))
        before = tuple(oms.db.iterdump())
        result = reconcile(oms, restated)
        assert result.status == "discrepancy"
        assert tuple(oms.db.iterdump()) == before


def test_equal_symbol_does_not_bind_a_different_market(tmp_path):
    with DurableOms(tmp_path / "market.sqlite") as oms:
        row = oms.create(replace(oms_order(), market=Market.USA), decision_id="foreign", now=NOW)
        oms.approve_risk(row.client_order_id, now=NOW)
        oms.submission_uncertain(row.client_order_id, reason="test", now=NOW)
        before = tuple(oms.db.iterdump())
        result = reconcile(oms, order_capture(), {row.client_order_id: "order-1"})
        assert result.status == "discrepancy"
        assert tuple(oms.db.iterdump()) == before


def test_two_local_orders_cannot_claim_one_broker_order(tmp_path):
    with DurableOms(tmp_path / "binding.sqlite") as oms:
        a = uncertain_order(oms, "a")
        b = uncertain_order(oms, "b")
        before = tuple(oms.db.iterdump())
        result = reconcile(oms, order_capture(), {a.client_order_id: "order-1", b.client_order_id: "order-1"})
        assert result.status == "discrepancy"
        assert tuple(oms.db.iterdump()) == before


def test_later_fill_failure_rolls_back_the_whole_observation(tmp_path, monkeypatch):
    with DurableOms(tmp_path / "atomic.sqlite") as oms:
        row = uncertain_order(oms)
        before = tuple(oms.db.iterdump())
        original = oms.fill
        calls = 0
        def failing_fill(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ValueError("synthetic_second_fill_failure")
            return original(*args, **kwargs)
        monkeypatch.setattr(oms, "fill", failing_fill)
        with pytest.raises(ValueError, match="synthetic_second_fill_failure"):
            reconcile(oms, order_capture(), {row.client_order_id: "order-1"})
        assert tuple(oms.db.iterdump()) == before
        assert oms.get(row.client_order_id).state is OrderState.SUBMISSION_UNCERTAIN


def test_projection_identity_tampering_cannot_pass_event_verification(tmp_path):
    with DurableOms(tmp_path / "projection.sqlite") as oms:
        row = uncertain_order(oms)
        with oms.db:
            oms.db.execute("UPDATE oms_orders SET symbol='TCS' WHERE client_order_id=?", (row.client_order_id,))
        with pytest.raises(ValueError, match="oms_identity_projection_mismatch"):
            oms.verify(row.client_order_id)


@pytest.mark.parametrize("field,value", [("scope", "another_account_scope"), ("currency", "USD"), ("segment", "commodity")])
def test_same_numbers_with_wrong_account_semantics_are_not_reconciled(field, value):
    snapshot = account_snapshot()
    expected = expected_account(snapshot)
    changed = deepcopy(snapshot)
    if field == "scope":
        changed[field] = value
    else:
        changed["funds"][field] = value
        changed["fundsBefore"][field] = value
    result = reconcile_external_account(resign(changed), expected, now=NOW)
    assert result.status == "unavailable"


@pytest.mark.parametrize("offset,code", [(301, "capture_stale"), (-1, "capture_from_future")])
def test_stale_or_future_observation_never_changes_orders(tmp_path, offset, code):
    capture = order_capture()
    with DurableOms(tmp_path / "clock.sqlite") as oms:
        row = uncertain_order(oms)
        before = tuple(oms.db.iterdump())
        result = OmsBrokerLifecycleReconciler().reconcile(
            oms, capture, tenant_id="tenant", account_ref=capture["accountRef"],
            bindings={row.client_order_id: "order-1"}, now=NOW + timedelta(seconds=offset),
        )
        assert result.status == "unavailable"
        assert result.issues[0].code == code
        assert tuple(oms.db.iterdump()) == before
        snapshot = account_snapshot()
        account_result = reconcile_external_account(
            snapshot, expected_account(snapshot), now=NOW + timedelta(seconds=offset),
        )
        assert account_result.status == "unavailable"
        assert account_result.issues == (code,)


@pytest.mark.parametrize("field,value", [
    ("accountRef", "b" * 64), ("instrumentId", "999"),
    ("exchange", "BSE"), ("product", "MIS"),
])
def test_account_and_contract_binding_survives_restart(tmp_path, field, value):
    path = tmp_path / "scoped.sqlite"
    capture = order_capture(OrderTransport(
        [broker_order(status="OPEN", filled_quantity=1, pending_quantity=2, average_price=0)],
        [broker_trade()],
    ))
    with DurableOms(path) as oms:
        row = uncertain_order(oms)
        assert reconcile(oms, capture, {row.client_order_id: "order-1"}).status == "matched"
    changed = deepcopy(capture)
    if field == "accountRef":
        changed[field] = value
    else:
        for collection in ("ordersBefore", "orders", "tradesBefore", "trades"):
            for item in changed[collection]:
                item[field] = value
    with DurableOms(path) as restarted:
        before = tuple(restarted.db.iterdump())
        result = reconcile(restarted, resign(changed))
        assert result.status == "discrepancy"
        assert result.issues[0].code == "broker_account_or_contract_binding_changed"
        assert tuple(restarted.db.iterdump()) == before
        assert restarted.verify(row.client_order_id)["verified"] is True


def test_matching_average_cannot_hide_restatement_of_individual_fills(tmp_path):
    with DurableOms(tmp_path / "restated.sqlite") as oms:
        row = uncertain_order(oms)
        assert reconcile(oms, order_capture(), {row.client_order_id: "order-1"}).status == "matched"
        restated = order_capture(OrderTransport(
            [broker_order()],
            [broker_trade(average_price=99),
             broker_trade(trade_id="trade-2", quantity=2, average_price="100.5")],
        ))
        before = tuple(oms.db.iterdump())
        result = reconcile(oms, restated)
        assert result.status == "discrepancy"
        assert result.issues[0].code == "broker_fill_payload_mismatch"
        assert tuple(oms.db.iterdump()) == before


def test_unknown_explicit_binding_is_not_silently_reported_matched(tmp_path):
    with DurableOms(tmp_path / "unknown.sqlite") as oms:
        result = reconcile(oms, order_capture(), {"OMS-unknown": "order-1"})
        assert result.status == "discrepancy"
        assert result.issues[0].code == "explicit_binding_order_unavailable"


def test_snapshot_check_and_mutation_hold_one_writer_lock(tmp_path, monkeypatch):
    import sqlite3
    path = tmp_path / "locking.sqlite"
    with DurableOms(path) as oms:
        row = uncertain_order(oms)
        other = sqlite3.connect(path, timeout=0)
        original = oms.all_orders
        def competing_read(tenant):
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("BEGIN IMMEDIATE")
            return original(tenant)
        monkeypatch.setattr(oms, "all_orders", competing_read)
        assert reconcile(oms, order_capture(), {row.client_order_id: "order-1"}).status == "matched"
        other.execute("BEGIN IMMEDIATE")
        other.rollback()
        other.close()


@pytest.mark.parametrize("column,value", [("state", "SUBMITTED"), ("broker_order_id", "forged")])
def test_reconciliation_rejects_tampered_state_or_broker_projection(tmp_path, column, value):
    with DurableOms(tmp_path / "state.sqlite") as oms:
        row = uncertain_order(oms)
        with oms.db:
            # Column names are the fixed local parametrization, never external input.
            oms.db.execute(f"UPDATE oms_orders SET {column}=? WHERE client_order_id=?", (value, row.client_order_id))
        before = tuple(oms.db.iterdump())
        result = reconcile(oms, order_capture(), {row.client_order_id: "order-1"})
        assert result.status == "discrepancy"
        assert result.issues[0].code.startswith("oms_integrity_invalid:")
        assert tuple(oms.db.iterdump()) == before
