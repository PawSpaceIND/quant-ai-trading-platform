from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal

from quant_ai.domain.models import AssetClass, Market, OrderIntent, Side
from quant_ai.execution.broker_lifecycle import (
    ExpectedBrokerAccount,
    ExpectedBrokerPosition,
    OmsBrokerLifecycleReconciler,
    reconcile_external_account,
)
from quant_ai.execution.broker_observation import capture_kite
from quant_ai.execution.external_account_snapshot import capture_external_account
from quant_ai.orders.oms import DurableOms
from quant_ai.orders.state import OrderState

NOW = datetime(2026, 9, 11, 6, 30, tzinfo=timezone.utc)
D = Decimal


def envelope(data):
    return {"status": "success", "data": data}


def broker_order(**changes):
    return {
        "order_id": "order-1", "instrument_token": 738561,
        "tradingsymbol": "RELIANCE", "exchange": "NSE", "product": "CNC",
        "transaction_type": "BUY", "quantity": 3, "filled_quantity": 3,
        "pending_quantity": 0, "cancelled_quantity": 0, "average_price": 100,
        "status": "COMPLETE", "variety": "regular",
        "order_timestamp": "2026-09-11 11:00:00", "exchange_order_id": "ex-1",
        **changes,
    }


def broker_trade(**changes):
    return {
        "trade_id": "trade-1", "order_id": "order-1", "instrument_token": 738561,
        "tradingsymbol": "RELIANCE", "exchange": "NSE", "product": "CNC",
        "transaction_type": "BUY", "quantity": 1, "average_price": 98,
        "fill_timestamp": "2026-09-11 11:01:00", "exchange_order_id": "ex-1",
        **changes,
    }


class OrderTransport:
    def __init__(self, orders=None, trades=None):
        self.orders = [broker_order()] if orders is None else orders
        self.trades = [
            broker_trade(), broker_trade(trade_id="trade-2", quantity=2, average_price=101)
        ] if trades is None else trades
        self.change = None
        self.calls = []

    def get(self, path, params=None):
        del params
        self.calls.append(path)
        result = {
            "/user/profile": envelope({"user_id": "AB1234"}),
            "/orders": envelope(self.orders),
            "/trades": envelope(self.trades),
        }[path]
        if self.change:
            return self.change(path, deepcopy(result), self.calls)
        return deepcopy(result)


def order_capture(transport=None):
    return capture_kite(
        transport or OrderTransport(), "AB1234", "tenant", clock=lambda: NOW
    )


def oms_order() -> OrderIntent:
    return OrderIntent(
        "RELIANCE", Market.INDIA, Side.BUY, 3, D(100), "strategy",
        AssetClass.EQUITY, "tenant", D(95), D(110),
    )


def uncertain_order(oms: DurableOms, decision_id="decision-1"):
    row = oms.create(oms_order(), decision_id=decision_id, now=NOW)
    oms.approve_risk(row.client_order_id, now=NOW)
    return oms.submission_uncertain(
        row.client_order_id, reason="network_timeout_after_send", now=NOW
    )


def test_explicit_binding_resolves_uncertain_split_fill_and_is_idempotent(tmp_path):
    capture = order_capture()
    with DurableOms(tmp_path / "oms.sqlite") as oms:
        row = uncertain_order(oms)
        report = OmsBrokerLifecycleReconciler().reconcile(
            oms, capture, tenant_id="tenant", account_ref=capture["accountRef"],
            bindings={row.client_order_id: "order-1"},
        )
        assert report.status == "matched"
        filled = oms.get(row.client_order_id)
        assert filled.state is OrderState.FILLED
        assert filled.filled_quantity == 3
        assert filled.average_fill_price == D(100)
        assert filled.broker_order_id == "order-1"
        again = OmsBrokerLifecycleReconciler().reconcile(
            oms, capture, tenant_id="tenant", account_ref=capture["accountRef"],
            bindings={row.client_order_id: "order-1"},
        )
        assert again.status == "matched"
        assert oms.verify(row.client_order_id)["verified"] is True


def test_partial_open_then_complete_capture_advances_without_duplicate_fill(tmp_path):
    open_capture = order_capture(OrderTransport(
        [broker_order(status="OPEN", filled_quantity=1, pending_quantity=2, average_price=0)],
        [broker_trade()],
    ))
    complete_capture = order_capture()
    with DurableOms(tmp_path / "oms.sqlite") as oms:
        row = uncertain_order(oms)
        first = OmsBrokerLifecycleReconciler().reconcile(
            oms, open_capture, tenant_id="tenant", account_ref=open_capture["accountRef"],
            bindings={row.client_order_id: "order-1"},
        )
        assert first.status == "matched"
        partial = oms.get(row.client_order_id)
        assert partial.state is OrderState.PARTIALLY_FILLED
        assert partial.filled_quantity == 1
        second = OmsBrokerLifecycleReconciler().reconcile(
            oms, complete_capture, tenant_id="tenant",
            account_ref=complete_capture["accountRef"],
        )
        assert second.status == "matched"
        assert oms.get(row.client_order_id).state is OrderState.FILLED
        assert oms.get(row.client_order_id).filled_quantity == 3


def test_cancelled_partial_and_rejected_orders_reconcile_terminal_state(tmp_path):
    cancelled_capture = order_capture(OrderTransport(
        [broker_order(status="CANCELLED", filled_quantity=1, pending_quantity=2,
                      cancelled_quantity=2, average_price=0)],
        [broker_trade()],
    ))
    with DurableOms(tmp_path / "cancel.sqlite") as oms:
        row = uncertain_order(oms)
        report = OmsBrokerLifecycleReconciler().reconcile(
            oms, cancelled_capture, tenant_id="tenant",
            account_ref=cancelled_capture["accountRef"],
            bindings={row.client_order_id: "order-1"},
        )
        assert report.status == "matched"
        current = oms.get(row.client_order_id)
        assert current.state is OrderState.CANCELLED and current.filled_quantity == 1

    rejected_capture = order_capture(OrderTransport(
        [broker_order(status="REJECTED", filled_quantity=0, pending_quantity=0,
                      cancelled_quantity=0, average_price=0)],
        [],
    ))
    with DurableOms(tmp_path / "reject.sqlite") as oms:
        row = uncertain_order(oms, "decision-rejected")
        report = OmsBrokerLifecycleReconciler().reconcile(
            oms, rejected_capture, tenant_id="tenant",
            account_ref=rejected_capture["accountRef"],
            bindings={row.client_order_id: "order-1"},
        )
        assert report.status == "matched"
        assert oms.get(row.client_order_id).state is OrderState.REJECTED


def test_uncertain_order_without_binding_is_never_guessed_from_economics(tmp_path):
    capture = order_capture()
    with DurableOms(tmp_path / "oms.sqlite") as oms:
        row = uncertain_order(oms)
        report = OmsBrokerLifecycleReconciler().reconcile(
            oms, capture, tenant_id="tenant", account_ref=capture["accountRef"]
        )
        assert report.status == "partial"
        assert report.unresolved[0].code == "broker_order_binding_required"
        assert oms.get(row.client_order_id).state is OrderState.SUBMISSION_UNCERTAIN
        assert oms.get(row.client_order_id).broker_order_id is None


def test_identity_mismatch_or_changing_capture_never_mutates_oms(tmp_path):
    wrong = order_capture(OrderTransport([broker_order(tradingsymbol="TCS")], [
        broker_trade(tradingsymbol="TCS"),
        broker_trade(trade_id="trade-2", quantity=2, average_price=101, tradingsymbol="TCS"),
    ]))
    with DurableOms(tmp_path / "wrong.sqlite") as oms:
        row = uncertain_order(oms)
        report = OmsBrokerLifecycleReconciler().reconcile(
            oms, wrong, tenant_id="tenant", account_ref=wrong["accountRef"],
            bindings={row.client_order_id: "order-1"},
        )
        assert report.status == "discrepancy"
        assert report.issues[0].code == "broker_order_identity_mismatch"
        assert oms.get(row.client_order_id).state is OrderState.SUBMISSION_UNCERTAIN

    changing_transport = OrderTransport()
    changing_transport.change = (
        lambda path, value, calls: envelope([broker_order(status="OPEN")])
        if path == "/orders" and calls.count(path) == 2 else value
    )
    changing = order_capture(changing_transport)
    with DurableOms(tmp_path / "changing.sqlite") as oms:
        row = uncertain_order(oms, "decision-changing")
        report = OmsBrokerLifecycleReconciler().reconcile(
            oms, changing, tenant_id="tenant", account_ref=changing["accountRef"],
            bindings={row.client_order_id: "order-1"},
        )
        assert report.status == "unavailable"
        assert oms.get(row.client_order_id).state is OrderState.SUBMISSION_UNCERTAIN


class AccountTransport:
    def __init__(self):
        self.profile = envelope({"user_id": "AB1234"})
        self.margins = envelope({
            "equity": {"enabled": True, "available": {
                "cash": 1000, "live_balance": 800, "opening_balance": 1200,
            }, "net": 900}
        })
        self.positions = envelope({"net": [{
            "instrument_token": 123, "tradingsymbol": "INFY", "exchange": "NSE",
            "product": "CNC", "quantity": 2, "average_price": 100,
        }]})

    def get(self, path):
        return deepcopy({
            "/user/profile": self.profile, "/user/margins": self.margins,
            "/portfolio/positions": self.positions,
        }[path])


def account_snapshot():
    return capture_external_account(
        AccountTransport(), "AB1234", "tenant", clock=lambda: NOW
    )


def expected_account(snapshot, *, cash="1000", available="800", quantity="2"):
    return ExpectedBrokerAccount(
        "tenant", snapshot["accountRef"], D(cash), D(available),
        (ExpectedBrokerPosition("123", "INFY", "NSE", "CNC", D(quantity), D(100)),),
    )


def test_external_cash_and_position_reconciliation_matches_exact_expected_book():
    snapshot = account_snapshot()
    report = reconcile_external_account(snapshot, expected_account(snapshot))
    assert report.status == "matched"
    assert report.issues == ()
    assert report.snapshot_sha256 == snapshot["sha256"]


def test_external_account_discrepancies_are_explicit_and_never_repaired():
    snapshot = account_snapshot()
    report = reconcile_external_account(
        snapshot, expected_account(snapshot, cash="999", available="799", quantity="3")
    )
    assert report.status == "discrepancy"
    assert set(report.issues) == {
        "cash_balance_mismatch", "available_balance_mismatch",
        "position_quantity_mismatch:123:NSE:CNC",
    }
    assert snapshot["funds"]["cash_balance"] == "1000"
    assert snapshot["positions"][0]["quantity"] == "2"


def test_external_snapshot_tamper_or_unstable_state_is_unavailable():
    snapshot = account_snapshot()
    tampered = deepcopy(snapshot)
    tampered["funds"]["cash_balance"] = "999"
    assert reconcile_external_account(
        tampered, expected_account(snapshot)
    ).status == "unavailable"

    unstable = deepcopy(snapshot)
    unstable["status"] = "changing"
    unsigned = {key: value for key, value in unstable.items() if key != "sha256"}
    import hashlib
    import json
    unstable["sha256"] = hashlib.sha256(json.dumps(
        unsigned, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    ).encode()).hexdigest()
    assert reconcile_external_account(
        unstable, expected_account(snapshot)
    ).issues == ("snapshot_not_consistent",)


def test_existing_nonbroker_fill_is_not_double_counted_and_ambiguous_delta_refuses(tmp_path):
    open_capture = order_capture(OrderTransport(
        [broker_order(status="OPEN", filled_quantity=1, pending_quantity=2, average_price=0)],
        [broker_trade()],
    ))
    complete_capture = order_capture()
    with DurableOms(tmp_path / "callback.sqlite") as oms:
        row = oms.create(oms_order(), decision_id="callback-fill", now=NOW)
        oms.approve_risk(row.client_order_id, now=NOW)
        oms.submitted(row.client_order_id, broker_order_id="order-1", now=NOW)
        oms.fill(
            row.client_order_id, fill_id="callback-fill-1", quantity=1,
            price=D(98), broker_order_id="order-1", now=NOW,
        )
        same_total = OmsBrokerLifecycleReconciler().reconcile(
            oms, open_capture, tenant_id="tenant", account_ref=open_capture["accountRef"]
        )
        assert same_total.status == "matched"
        assert oms.get(row.client_order_id).filled_quantity == 1
        ambiguous = OmsBrokerLifecycleReconciler().reconcile(
            oms, complete_capture, tenant_id="tenant",
            account_ref=complete_capture["accountRef"],
        )
        assert ambiguous.status == "discrepancy"
        assert ambiguous.issues[0].code == "broker_fill_lineage_ambiguous"
        assert oms.get(row.client_order_id).filled_quantity == 1
