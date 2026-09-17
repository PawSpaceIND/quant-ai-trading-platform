from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant_ai.domain.models import AssetClass, Market, OrderIntent, Side
from quant_ai.orders.oms import DurableOms
from quant_ai.orders.state import OrderState

NOW = datetime(2026, 9, 16, 10, tzinfo=timezone.utc)


def order(quantity=10):
    return OrderIntent(
        "INFY", Market.INDIA, Side.BUY, quantity, Decimal(1500), "strategy-a",
        AssetClass.EQUITY, "tenant-a", Decimal(1450), Decimal(1600),
    )


def test_order_and_partial_fills_survive_restart_with_exact_average(tmp_path):
    path = tmp_path / "oms.sqlite"
    with DurableOms(path) as oms:
        created = oms.create(order(), decision_id="decision-1", now=NOW)
        assert created.state is OrderState.CREATED
        oms.approve_risk(created.client_order_id, now=NOW + timedelta(seconds=1))
        oms.submitted(created.client_order_id, broker_order_id="broker-1",
                      now=NOW + timedelta(seconds=2))
        partial = oms.fill(created.client_order_id, fill_id="fill-1", quantity=4,
                           price=Decimal("1501.25"), broker_order_id="broker-1",
                           now=NOW + timedelta(seconds=3))
        assert partial.state is OrderState.PARTIALLY_FILLED
        assert partial.pending_quantity == 6
    with DurableOms(path) as restarted:
        restored = restarted.get(created.client_order_id)
        assert restored.state is OrderState.PARTIALLY_FILLED
        filled = restarted.fill(created.client_order_id, fill_id="fill-2", quantity=6,
                                price=Decimal("1502.75"), broker_order_id="broker-1",
                                now=NOW + timedelta(seconds=4))
        assert filled.state is OrderState.FILLED
        assert filled.filled_quantity == 10
        assert filled.average_fill_price == Decimal("1502.15")
        assert restarted.verify(created.client_order_id)["verified"] is True


def test_client_order_and_fill_idempotency_never_duplicates_economic_state(tmp_path):
    with DurableOms(tmp_path / "oms.sqlite") as oms:
        first = oms.create(order(), decision_id="decision-1", now=NOW)
        same = oms.create(order(), decision_id="decision-1", now=NOW + timedelta(seconds=1))
        assert same.client_order_id == first.client_order_id
        assert same.last_event_sequence == 1
        oms.approve_risk(first.client_order_id, now=NOW + timedelta(seconds=2))
        oms.submitted(first.client_order_id, broker_order_id="broker-1",
                      now=NOW + timedelta(seconds=3))
        a = oms.fill(first.client_order_id, fill_id="fill-1", quantity=5,
                     price=Decimal(1500), broker_order_id="broker-1",
                     now=NOW + timedelta(seconds=4))
        b = oms.fill(first.client_order_id, fill_id="fill-1", quantity=5,
                     price=Decimal(1500), broker_order_id="broker-1",
                     now=NOW + timedelta(seconds=5))
        assert a.filled_quantity == b.filled_quantity == 5
        with pytest.raises(ValueError, match="fill_id_payload_mismatch"):
            oms.fill(first.client_order_id, fill_id="fill-1", quantity=4,
                     price=Decimal(1500), broker_order_id="broker-1")


def test_uncertain_submission_is_visible_and_only_observation_resolves_it(tmp_path):
    with DurableOms(tmp_path / "oms.sqlite") as oms:
        row = oms.create(order(), decision_id="decision-1", now=NOW)
        oms.approve_risk(row.client_order_id, now=NOW + timedelta(seconds=1))
        uncertain = oms.submission_uncertain(
            row.client_order_id, reason="network_timeout_after_send",
            now=NOW + timedelta(seconds=2),
        )
        assert uncertain.state is OrderState.SUBMISSION_UNCERTAIN
        assert oms.open_orders("tenant-a") == (uncertain,)
        resolved = oms.submitted(row.client_order_id, broker_order_id="broker-seen",
                                 now=NOW + timedelta(seconds=3))
        assert resolved.state is OrderState.SUBMITTED
        assert resolved.broker_order_id == "broker-seen"


def test_terminal_identity_overfill_and_illegal_transition_fail_closed(tmp_path):
    with DurableOms(tmp_path / "oms.sqlite") as oms:
        row = oms.create(order(3), decision_id="decision-1", now=NOW)
        oms.approve_risk(row.client_order_id)
        oms.submitted(row.client_order_id, broker_order_id="broker-1")
        with pytest.raises(ValueError, match="fill_exceeds_requested_quantity"):
            oms.fill(row.client_order_id, fill_id="too-much", quantity=4, price=Decimal(1))
        oms.fill(row.client_order_id, fill_id="all", quantity=3, price=Decimal(1500),
                 broker_order_id="broker-1")
        with pytest.raises(ValueError, match="invalid order transition FILLED->CANCELLED"):
            oms.cancel(row.client_order_id, reason="too late")
        with pytest.raises(ValueError, match="broker_order_identity_changed"):
            # Use a fresh order whose broker identity is already bound.
            fresh = oms.create(order(2), decision_id="decision-2")
            oms.approve_risk(fresh.client_order_id)
            oms.submitted(fresh.client_order_id, broker_order_id="broker-a")
            oms.fill(fresh.client_order_id, fill_id="x", quantity=1, price=Decimal(1500),
                     broker_order_id="broker-b")


def test_event_history_is_append_only_and_projection_tampering_is_detected(tmp_path):
    with DurableOms(tmp_path / "oms.sqlite") as oms:
        row = oms.create(order(), decision_id="decision-1", now=NOW)
        oms.approve_risk(row.client_order_id)
        with pytest.raises(Exception, match="append-only"):
            oms.db.execute("DELETE FROM oms_events")
        oms.db.execute(
            "UPDATE oms_orders SET filled_quantity=1 WHERE client_order_id=?", (row.client_order_id,)
        )
        with pytest.raises(ValueError, match="oms_fill_projection_mismatch"):
            oms.verify(row.client_order_id)


def test_cancel_replace_is_durable_bounded_and_restart_safe(tmp_path) -> None:
    path = tmp_path / "oms-replace.sqlite"
    with DurableOms(path) as oms:
        original = oms.create(order(10), decision_id="decision-original", now=NOW)
        oms.approve_risk(original.client_order_id, now=NOW + timedelta(seconds=1))
        oms.submitted(
            original.client_order_id,
            broker_order_id="broker-original",
            now=NOW + timedelta(seconds=2),
        )
        partial = oms.fill(
            original.client_order_id,
            fill_id="partial-before-cancel",
            quantity=4,
            price=Decimal(1500),
            broker_order_id="broker-original",
            now=NOW + timedelta(seconds=3),
        )
        assert partial.pending_quantity == 6
        cancelled = oms.cancel(
            original.client_order_id,
            reason="operator_replace_price",
            now=NOW + timedelta(seconds=4),
        )
        assert cancelled.state is OrderState.CANCELLED
        replacement_order = OrderIntent(
            "INFY", Market.INDIA, Side.BUY, 6, Decimal(1495), "strategy-a",
            AssetClass.EQUITY, "tenant-a", Decimal(1450), Decimal(1600),
        )
        replacement = oms.replace_cancelled(
            original.client_order_id,
            replacement_order,
            decision_id="decision-replacement",
            reason="operator_replace_price",
            now=NOW + timedelta(seconds=5),
        )
        assert replacement.state is OrderState.CREATED
        assert replacement.requested_quantity == 6
        assert oms.replacement_for(original.client_order_id) == replacement.client_order_id
        assert oms.replacement_parent(replacement.client_order_id) == original.client_order_id
        assert oms.verify(original.client_order_id)["verified"] is True
        assert oms.verify(replacement.client_order_id)["verified"] is True

    with DurableOms(path) as restarted:
        restored = restarted.get(replacement.client_order_id)
        assert restarted.replacement_parent(restored.client_order_id) == original.client_order_id
        same = restarted.replace_cancelled(
            original.client_order_id,
            replacement_order,
            decision_id="decision-replacement",
            reason="operator_replace_price",
            now=NOW + timedelta(seconds=6),
        )
        assert same.client_order_id == restored.client_order_id
        assert same.last_event_sequence == restored.last_event_sequence


def test_replacement_refuses_race_overfill_and_identity_change(tmp_path) -> None:
    with DurableOms(tmp_path / "oms-replace.sqlite") as oms:
        original = oms.create(order(10), decision_id="decision-original", now=NOW)
        oms.approve_risk(original.client_order_id)
        oms.submitted(original.client_order_id, broker_order_id="broker-original")
        with pytest.raises(ValueError, match="replacement_requires_confirmed_cancel"):
            oms.replace_cancelled(
                original.client_order_id,
                order(10),
                decision_id="replacement-too-early",
                reason="not_cancelled_yet",
            )
        oms.fill(
            original.client_order_id,
            fill_id="partial-before-cancel",
            quantity=4,
            price=Decimal(1500),
            broker_order_id="broker-original",
        )
        oms.cancel(original.client_order_id, reason="replace")
        with pytest.raises(ValueError, match="replacement_exceeds_cancelled_remainder"):
            oms.replace_cancelled(
                original.client_order_id,
                order(7),
                decision_id="replacement-too-large",
                reason="replace",
            )
        changed_symbol = OrderIntent(
            "TCS", Market.INDIA, Side.BUY, 6, Decimal(1495), "strategy-a",
            AssetClass.EQUITY, "tenant-a", Decimal(1450), Decimal(1600),
        )
        with pytest.raises(ValueError, match="replacement_economic_identity_changed"):
            oms.replace_cancelled(
                original.client_order_id,
                changed_symbol,
                decision_id="replacement-wrong-symbol",
                reason="replace",
            )
