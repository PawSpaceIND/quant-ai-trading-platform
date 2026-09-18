"""Execution intent cannot change across risk, OMS, replacement or restart."""
from dataclasses import replace
from datetime import date

import pytest
from test_institutional_paper_coordinator import NOW, D, Harness, make_request

from quant_ai.domain.models import (
    AssetClass,
    Instrument,
    InstrumentBoundOrderIntent,
    Market,
    OrderIntent,
    Side,
)
from quant_ai.orders.oms import DurableOms


@pytest.mark.parametrize("field,value", [
    ("side", Side.SELL), ("market", Market.USA), ("asset_class", AssetClass.ETF),
    ("strategy_id", "another-strategy"), ("reference_price", D(101)),
    ("stop_price", D(90)), ("take_profit_price", D(115)),
])
def test_restart_rebinding_rejects_changed_parent_intent(tmp_path, field, value):
    h = Harness(tmp_path)
    try:
        request = make_request(h.broker)
        prepared = h.coordinator.prepare(request)
        assert prepared.approved
        changed = replace(prepared.approved_order, **{field: value})
        with pytest.raises(ValueError, match="execution_program_runtime_context_mismatch"):
            h.coordinator.bind_runtime_context(prepared.program.program_id,
                request=request, parent_order=changed)
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        h.close()


def bound_future(expiry=date(2026, 12, 5)):
    instrument = Instrument("SYNTH26DECFUT", Market.INDIA, AssetClass.METAL,
        "INR", "MCX", True, {"source": "synthetic identity test"}, expiry, 10, D(1), "SYNTH")
    return InstrumentBoundOrderIntent(instrument.symbol, instrument.market, Side.BUY,
        10, D(100), "strategy", instrument.asset_class, "tenant", D(90), D(120), instrument)


def test_contract_change_cannot_reuse_same_oms_identity(tmp_path):
    with DurableOms(tmp_path / "oms.sqlite") as oms:
        a = oms.create(bound_future(), decision_id="same-decision", now=NOW)
        b = oms.create(bound_future(date(2027, 1, 5)), decision_id="same-decision", now=NOW)
        assert a.client_order_id != b.client_order_id


def test_cancel_replace_does_not_change_bound_contract(tmp_path):
    with DurableOms(tmp_path / "oms.sqlite") as oms:
        original = oms.create(bound_future(), decision_id="original", now=NOW)
        oms.approve_risk(original.client_order_id, now=NOW)
        oms.cancel(original.client_order_id, reason="synthetic cancel", now=NOW)
        before = tuple(oms.db.iterdump())
        with pytest.raises(ValueError, match="replacement_economic_identity_changed"):
            oms.replace_cancelled(original.client_order_id,
                bound_future(date(2027, 1, 5)), decision_id="replacement",
                reason="cannot silently roll", now=NOW)
        assert tuple(oms.db.iterdump()) == before


def test_changed_protective_levels_do_not_pass_as_same_oms_intent(tmp_path):
    plain = OrderIntent("INFY", Market.INDIA, Side.BUY, 1, D(100), "strategy",
                        AssetClass.EQUITY, "tenant", D(95), D(110))
    with DurableOms(tmp_path / "oms.sqlite") as oms:
        oms.create(plain, decision_id="same-decision", now=NOW)
        with pytest.raises(ValueError, match="client_order_id_intent_mismatch"):
            oms.create(replace(plain, stop_price=D(90)), decision_id="same-decision", now=NOW)



def test_mutating_saved_request_inputs_is_detected_before_any_slice(tmp_path):
    h = Harness(tmp_path)
    try:
        request = make_request(h.broker)
        prepared = h.coordinator.prepare(request)
        request.current_strategy_weights["unexpected"] = D("0.5")
        with pytest.raises(ValueError, match="execution_program_runtime_context_mismatch"):
            h.coordinator.execute_due(prepared.program.program_id, now=NOW)
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        h.close()


def test_program_parent_payload_cannot_be_overwritten_after_approval(tmp_path):
    h = Harness(tmp_path)
    try:
        prepared = h.coordinator.prepare(make_request(h.broker))
        with pytest.raises(Exception, match="immutable"):
            h.programs.db.execute("UPDATE execution_programs SET parent_order_payload='{}'")
        assert h.programs.get(prepared.program.program_id).parent_order_payload != "{}"
    finally:
        h.close()


def bound_request(h):
    from test_institutional_paper_coordinator import proposal

    from quant_ai.agents.swarm import InstrumentBoundTradeProposal
    instrument = Instrument("INFY", Market.INDIA, AssetClass.EQUITY, "INR", "NSE")
    return make_request(h.broker, p=InstrumentBoundTradeProposal(**vars(proposal()), instrument=instrument))


@pytest.mark.parametrize("defect", ["contract", "quantity", "oms_link"])
def test_accounting_recovery_refuses_changed_fill_attribution(tmp_path, defect):
    import json

    from test_institutional_paper_coordinator import FailOnceAccounting
    h = Harness(tmp_path, accounting_cls=FailOnceAccounting)
    try:
        request = bound_request(h)
        prepared = h.coordinator.prepare(request)
        result = h.coordinator.execute_due(prepared.program.program_id, now=NOW)
        assert "accounting_reconciliation_required" in result.reason
        parent = prepared.approved_order
        assert len(h.broker.ledger_entries("tenant")) == 1
    finally:
        h.close()
    h = Harness(tmp_path)
    try:
        h.coordinator.bind_runtime_context(prepared.program.program_id, request=request, parent_order=parent)
        if defect == "contract":
            row = h.broker._connection.execute("SELECT instrument_identity FROM paper_ledger").fetchone()
            changed = json.loads(row[0]); changed["exchange"] = "BSE"
            with h.broker._connection:
                h.broker._connection.execute("UPDATE paper_ledger SET instrument_identity=?", (json.dumps(changed),))
        elif defect == "quantity":
            with h.broker._connection:
                h.broker._connection.execute("UPDATE paper_ledger SET quantity=9")
        else:
            other = h.oms.create(parent, decision_id="unrelated", now=NOW)
            with h.programs.db:
                h.programs.db.execute("UPDATE execution_program_slices SET client_order_id=?",
                                      (other.client_order_id,))
        with pytest.raises(ValueError, match="accounting_recovery_"):
            h.coordinator.reconcile_accounting(prepared.program.program_id)
        assert h.journal.native_balance("tenant", "CASH_AVAILABLE", "INR") == D(100000)
        assert len(h.broker.ledger_entries("tenant")) == 1
    finally:
        h.close()


def test_full_approved_intent_survives_oms_restart(tmp_path):
    path = tmp_path / "complete-intent.sqlite"
    order = bound_future()
    with DurableOms(path) as oms:
        created = oms.create(order, decision_id="full-intent", now=NOW)
        with pytest.raises(Exception, match="append-only"):
            oms.db.execute("UPDATE oms_events SET payload='{}'")
    with DurableOms(path) as oms:
        recovered = oms.get_intent(created.client_order_id)
        assert isinstance(recovered, InstrumentBoundOrderIntent)
        assert recovered == order
        assert recovered.stop_price == D(90)
        assert recovered.take_profit_price == D(120)
        assert recovered.instrument.expiry == date(2026, 12, 5)
        assert oms.verify(created.client_order_id)["verified"]
        with pytest.raises(TypeError, match="immutable"):
            recovered.instrument.metadata["source"] = "changed"


def test_legacy_creation_does_not_invent_missing_protective_intent(tmp_path, monkeypatch):
    with DurableOms(tmp_path / "legacy-intent.sqlite") as oms:
        append = oms._append_event_locked
        def legacy_append(client_id, kind, at, payload):
            payload = dict(payload)
            payload.pop("approvedIntent", None)
            return append(client_id, kind, at, payload)
        with monkeypatch.context() as patch:
            patch.setattr(oms, "_append_event_locked", legacy_append)
            row = oms.create(bound_future(), decision_id="legacy", now=NOW)
        assert oms.intent_snapshot(row.client_order_id) is None
        assert oms.verify(row.client_order_id)["verified"]
        with pytest.raises(ValueError, match="oms_legacy_full_intent_unavailable"):
            oms.get_intent(row.client_order_id)


def test_correctly_hashed_inconsistent_full_intent_fails_replay(tmp_path, monkeypatch):
    from quant_ai.orders.intent import canonical_order_intent
    with DurableOms(tmp_path / "bad-writer.sqlite") as oms:
        append = oms._append_event_locked
        def bad_append(client_id, kind, at, payload):
            if kind == "CREATED":
                payload = {**payload, "approvedIntent": canonical_order_intent(replace(bound_future(), side=Side.SELL))}
            return append(client_id, kind, at, payload)
        with monkeypatch.context() as patch:
            patch.setattr(oms, "_append_event_locked", bad_append)
            row = oms.create(bound_future(), decision_id="defective-writer", now=NOW)
        with pytest.raises(ValueError, match="oms_full_intent_projection_mismatch"):
            oms.verify(row.client_order_id)


def test_bound_accounting_recovery_closes_once_after_restart(tmp_path):
    import hashlib
    import json

    from test_institutional_paper_coordinator import FailOnceAccounting

    from quant_ai.orders.intent import canonical_order_intent
    h = Harness(tmp_path, accounting_cls=FailOnceAccounting)
    try:
        request = bound_request(h)
        prepared = h.coordinator.prepare(request)
        result = h.coordinator.execute_due(prepared.program.program_id, now=NOW)
        assert "accounting_reconciliation_required" in result.reason
        parent = prepared.approved_order
    finally:
        h.close()
    h = Harness(tmp_path)
    try:
        h.coordinator.bind_runtime_context(prepared.program.program_id, request=request, parent_order=parent)
        result = h.coordinator.reconcile_accounting(prepared.program.program_id)
        assert result.stage.value == "COMPLETE"
        client_id = result.program.slices[0].client_order_id
        assert h.oms.get_intent(client_id) == parent
        assert h.oms.verify(client_id)["verified"]
        ledger = h.broker.ledger_entries("tenant")[0]
        assert ledger.instrument_identity == h.oms.get(client_id).instrument_identity
        evidence = json.loads(h.broker._connection.execute(
            "SELECT payload FROM paper_decision_evidence WHERE order_id=?", (ledger.order_id,)
        ).fetchone()[0])
        assert evidence["order_intent_sha256"] == hashlib.sha256(canonical_order_intent(parent).encode()).hexdigest()
        assert evidence["instrument_identity"] == ledger.instrument_identity
        for _ in range(2):
            h.coordinator.reconcile_accounting(prepared.program.program_id)
            assert h.journal.native_balance("tenant", "CASH_AVAILABLE", "INR") == D(99000)
            assert h.journal.native_balance("tenant", "SECURITIES_COST", "INR") == D(1000)
            assert len(h.broker.ledger_entries("tenant")) == 1
        assert h.journal.verify("tenant")["verified"]
    finally:
        h.close()


@pytest.mark.parametrize("field,value", [("quantity", True), ("reference_price", D("NaN")),
    ("stop_price", D("Infinity")), ("strategy_id", "bad\nstrategy")])
def test_full_intent_snapshot_rejects_invalid_fields(field, value):
    from quant_ai.orders.intent import canonical_order_intent
    order = OrderIntent("INFY", Market.INDIA, Side.BUY, 1, D(100), "strategy")
    with pytest.raises(ValueError, match="order_snapshot_"):
        canonical_order_intent(replace(order, **{field: value}))
