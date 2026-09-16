"""Synthetic imported trades must remain isolated across accounts and trading days."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from test_broker_lifecycle_reconciliation import NOW, D, oms_order, order_capture
from test_broker_reconciliation_hardening import resign

from quant_ai.execution.broker_lifecycle import OmsBrokerLifecycleReconciler
from quant_ai.orders.oms import DurableOms


def scoped_capture(*, tenant="tenant", account=None, days=0, order_id="order-1"):
    capture = deepcopy(order_capture())
    capture["tenantId"] = tenant
    if account is not None:
        capture["accountRef"] = account
    for key in ("startedAt", "finishedAt"):
        capture[key] = (datetime.fromisoformat(capture[key]) + timedelta(days=days)).isoformat(timespec="milliseconds")
    for key in ("ordersBefore", "orders", "tradesBefore", "trades"):
        for row in capture[key]:
            row["at"] = (datetime.fromisoformat(row["at"]) + timedelta(days=days)).isoformat()
            row["orderId"] = order_id
    return resign(capture)


def reconcile(oms, capture, client_id=None):
    return OmsBrokerLifecycleReconciler().reconcile(
        oms, capture, tenant_id=capture["tenantId"], account_ref=capture["accountRef"],
        bindings={client_id: capture["orders"][0]["orderId"]} if client_id else None,
        now=datetime.fromisoformat(capture["finishedAt"]),
    )


def create_order(oms, capture, decision):
    row = oms.create(replace(oms_order(), tenant_id=capture["tenantId"]),
                     decision_id=decision, now=datetime.fromisoformat(capture["startedAt"]))
    oms.approve_risk(row.client_order_id, now=NOW)
    oms.submission_uncertain(row.client_order_id, reason="synthetic timeout", now=NOW)
    return row


@pytest.mark.parametrize("dimension", ["account", "day", "tenant"])
def test_reused_exchange_trade_id_does_not_collide_across_scope(tmp_path, dimension):
    first = scoped_capture()
    changes = {"account": {"tenant": "other", "account": "b" * 64},
               "day": {"days": 1, "order_id": "order-2"},
               "tenant": {"tenant": "other", "order_id": "order-2"}}[dimension]
    second = scoped_capture(**changes)
    path = tmp_path / "oms.sqlite"
    with DurableOms(path) as oms:
        a = create_order(oms, first, "a")
        assert reconcile(oms, first, a.client_order_id).status == "matched"
        original_ids = oms.fill_ids(a.client_order_id)
    with DurableOms(path) as oms:
        b = create_order(oms, second, "b")
        assert reconcile(oms, second, b.client_order_id).status == "matched"
        assert set(original_ids).isdisjoint(oms.fill_ids(b.client_order_id))
        assert len(oms.fill_ids(b.client_order_id)) == 2
        assert oms.get(b.client_order_id).average_fill_price == D(100)
        before = tuple(oms.db.iterdump())
        assert reconcile(oms, second, b.client_order_id).status == "matched"
        assert tuple(oms.db.iterdump()) == before
        assert oms.verify(a.client_order_id)["verified"]
        assert oms.verify(b.client_order_id)["verified"]


def test_trade_timestamp_restatement_is_not_same_execution(tmp_path):
    capture = scoped_capture()
    with DurableOms(tmp_path / "time.sqlite") as oms:
        row = create_order(oms, capture, "a")
        assert reconcile(oms, capture, row.client_order_id).status == "matched"
        changed = deepcopy(capture)
        for key in ("tradesBefore", "trades"):
            changed[key][0]["at"] = (datetime.fromisoformat(changed[key][0]["at"]) + timedelta(seconds=1)).isoformat()
        before = tuple(oms.db.iterdump())
        result = reconcile(oms, resign(changed), row.client_order_id)
        assert result.status == "discrepancy"
        assert result.issues[0].code == "broker_fill_payload_mismatch"
        assert tuple(oms.db.iterdump()) == before


def test_namespaced_execution_events_retain_provider_identity(tmp_path):
    import json
    capture = scoped_capture()
    with DurableOms(tmp_path / "evidence.sqlite") as oms:
        row = create_order(oms, capture, "a")
        assert reconcile(oms, capture, row.client_order_id).status == "matched"
        events = oms.db.execute("SELECT payload FROM oms_events WHERE kind='FILL'").fetchall()
        sources = [json.loads(event[0])["sourceIdentity"] for event in events]
        assert {source["tradeId"] for source in sources} == {"trade-1", "trade-2"}
        assert all(source["accountRef"] == capture["accountRef"] for source in sources)
        assert all(source["tradingDay"] == "2026-09-11" for source in sources)
        assert all(source["tenantId"] == "tenant" for source in sources)


@pytest.mark.parametrize("bound", [True, False])
def test_legacy_fill_ids_are_not_rewritten_or_replayed(tmp_path, bound):
    from quant_ai.execution.broker_lifecycle import _evidence_binding
    capture = scoped_capture()
    path = tmp_path / "legacy.sqlite"
    with DurableOms(path) as oms:
        row = create_order(oms, capture, "legacy")
        oms.submitted(row.client_order_id, broker_order_id="order-1", now=NOW)
        if bound:
            oms.bind_broker_evidence(row.client_order_id,
                                    _evidence_binding(capture, capture["orders"][0]), now=NOW)
        for trade in capture["trades"]:
            oms.fill(row.client_order_id, fill_id=f"KITE:NSE:{trade['tradeId']}",
                     quantity=trade["quantity"], price=D(trade["price"]),
                     broker_order_id="order-1", now=datetime.fromisoformat(trade["at"]))
    with DurableOms(path) as oms:
        before = tuple(oms.db.iterdump())
        result = reconcile(oms, capture, row.client_order_id)
        assert result.status == ("matched" if bound else "discrepancy")
        if not bound:
            assert result.issues[0].code == "legacy_broker_fill_scope_unverified"
        assert tuple(oms.db.iterdump()) == before
        assert oms.fill_ids(row.client_order_id) == ("KITE:NSE:trade-1", "KITE:NSE:trade-2")
        assert oms.verify(row.client_order_id)["verified"]


def test_legacy_partial_fill_can_finish_with_new_namespaced_fill(tmp_path):
    from quant_ai.execution.broker_lifecycle import _evidence_binding
    capture = scoped_capture()
    with DurableOms(tmp_path / "legacy-partial.sqlite") as oms:
        row = create_order(oms, capture, "legacy")
        oms.submitted(row.client_order_id, broker_order_id="order-1", now=NOW)
        oms.bind_broker_evidence(row.client_order_id,
                                _evidence_binding(capture, capture["orders"][0]), now=NOW)
        trade = capture["trades"][0]
        oms.fill(row.client_order_id, fill_id="KITE:NSE:trade-1", quantity=1, price=D(98),
                 broker_order_id="order-1", now=datetime.fromisoformat(trade["at"]))
        assert reconcile(oms, capture).status == "matched"
        assert oms.get(row.client_order_id).filled_quantity == 3
        assert len(oms.fill_ids(row.client_order_id)) == 2
        assert "KITE:NSE:trade-1" in oms.fill_ids(row.client_order_id)
        assert any(item.startswith("KITE2:") for item in oms.fill_ids(row.client_order_id))
        before = tuple(oms.db.iterdump())
        assert reconcile(oms, capture).status == "matched"
        assert tuple(oms.db.iterdump()) == before


@pytest.mark.parametrize("field,value", [("accountRef", "invalid"), ("tradingDay", "20260911"),
                                         ("broker", "unknown"), ("tradeId", "bad\nvalue")])
def test_malformed_external_execution_identity_is_refused(field, value):
    from quant_ai.execution.broker_lifecycle import _fill_source
    from quant_ai.orders.execution_identity import external_fill_id
    capture = scoped_capture()
    source = _fill_source(capture["trades"][0], capture)
    source[field] = value
    with pytest.raises(ValueError):
        external_fill_id(source)


@pytest.mark.parametrize("field,value", [("tenantId", "other"), ("accountRef", "b" * 64),
    ("tradingDay", "2026-09-12"), ("brokerOrderId", "order-2"), ("exchange", "BSE")])
def test_each_namespace_dimension_changes_fill_identity(field, value):
    from quant_ai.execution.broker_lifecycle import _fill_source
    from quant_ai.orders.execution_identity import external_fill_id
    capture = scoped_capture()
    source = _fill_source(capture["trades"][0], capture)
    assert external_fill_id(source) != external_fill_id({**source, field: value})
