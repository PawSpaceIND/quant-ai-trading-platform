"""Synthetic contract identity, not additional market/asset admission."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, timedelta

import pytest
from test_institutional_paper_coordinator import NOW, D, Harness, make_request, proposal

from quant_ai.agents.swarm import InstrumentBoundTradeProposal
from quant_ai.domain.models import AssetClass, Instrument, InstrumentBoundOrderIntent, Market, Side
from quant_ai.execution.planner import (
    ExecutionAlgorithm,
    ExecutionConstraints,
    ExecutionPlanner,
    VolumeBucket,
)
from quant_ai.instruments.identity import canonical_instrument_identity
from quant_ai.orders.oms import DurableOms


def cash_instrument(**changes):
    return replace(Instrument("INFY", Market.INDIA, AssetClass.EQUITY, "INR", "NSE",
                              lot_size=1, tick_size=D("0.05")), **changes)


def future(**changes):
    return replace(Instrument("SYNTHETIC_FUT", Market.INDIA, AssetClass.METAL, "INR", "MCX",
                              expiry=date(2026, 12, 5), lot_size=10, tick_size=D(1),
                              underlying="SYNTHETIC"), **changes)


def bound_order(instrument=None):
    instrument = instrument or future()
    return InstrumentBoundOrderIntent(instrument.symbol, instrument.market, Side.BUY, 10,
        D(100), "strategy", instrument.asset_class, "tenant", D(95), D(110), instrument)


BoundTestProposal = InstrumentBoundTradeProposal


@pytest.mark.parametrize("change", [{"expiry": date(2026, 12, 6)}, {"lot_size": 5},
                                    {"tick_size": D("0.5")}, {"metadata": {"source": "different"}}])
def test_oms_client_identity_includes_full_contract(change):
    assert DurableOms.client_order_id(bound_order(), "decision") != DurableOms.client_order_id(
        bound_order(future(**change)), "decision")


def test_oms_persists_contract_snapshot_and_verifies_it_after_restart(tmp_path):
    path = tmp_path / "oms.sqlite"
    order = bound_order()
    with DurableOms(path) as oms:
        row = oms.create(order, decision_id="bound", now=NOW)
    with DurableOms(path) as oms:
        restored = oms.get(row.client_order_id)
        assert getattr(restored, "instrument_identity", None) == canonical_instrument_identity(order.instrument)
        assert oms.verify(row.client_order_id)["verified"]
        event = json.loads(oms.db.execute("SELECT payload FROM oms_events WHERE sequence=1").fetchone()[0])
        assert event["order"]["instrumentIdentity"] == canonical_instrument_identity(order.instrument)


def test_replacement_cannot_change_expiry_under_same_symbol(tmp_path):
    with DurableOms(tmp_path / "oms.sqlite") as oms:
        row = oms.create(bound_order(), decision_id="bound", now=NOW)
        oms.cancel(row.client_order_id, reason="synthetic", now=NOW)
        before = tuple(oms.db.iterdump())
        with pytest.raises(ValueError, match="replacement_economic_identity_changed"):
            oms.replace_cancelled(row.client_order_id,
                bound_order(future(expiry=date(2026, 12, 6))), decision_id="replacement", reason="test", now=NOW)
        assert tuple(oms.db.iterdump()) == before


def test_planner_cannot_slice_against_wrong_contract_lot():
    with pytest.raises(ValueError, match="execution_contract_lot_mismatch"):
        ExecutionPlanner().plan(bound_order(), algorithm=ExecutionAlgorithm.TWAP,
            constraints=ExecutionConstraints(lot_size=1),
            buckets=(VolumeBucket(NOW, 1000), VolumeBucket(NOW + timedelta(minutes=1), 1000)))


@pytest.mark.parametrize("changes", [{"side": Side.SELL}, {"reference_price": D(101)},
    {"stop_price": D(90)}, {"strategy_id": "different"}, {"market": Market.USA}])
def test_restart_binding_cannot_change_parent_economics(tmp_path, changes):
    h = Harness(tmp_path)
    try:
        request = make_request(h.broker)
        prepared = h.coordinator.prepare(request)
        with pytest.raises(ValueError, match="execution_program_runtime_context_mismatch"):
            h.coordinator.bind_runtime_context(prepared.program.program_id, request=request,
                parent_order=replace(prepared.approved_order, **changes))
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        h.close()


def test_bound_cash_proposal_survives_risk_oms_paper_fill_and_accounting(tmp_path):
    h = Harness(tmp_path)
    try:
        p = BoundTestProposal(**vars(proposal()), instrument=cash_instrument())
        request = make_request(h.broker, p=p)
        prepared = h.coordinator.prepare(request)
        assert prepared.approved
        assert getattr(prepared.approved_order, "instrument", None) == p.instrument
        result = h.coordinator.execute_due(prepared.program.program_id, now=NOW)
        assert result.program.state.value == "COMPLETE"
        row = h.oms.get(result.program.slices[0].client_order_id)
        assert row.instrument_identity == canonical_instrument_identity(p.instrument)
        saved = h.broker._connection.execute("SELECT instrument_identity FROM paper_ledger").fetchone()[0]
        assert saved == row.instrument_identity
        assert h.broker.get_margin("tenant").cash_balance == h.journal.native_balance("tenant", "CASH_AVAILABLE", "INR")
    finally:
        h.close()


def test_real_pipeline_factory_carries_identity_without_global_lookup(tmp_path):
    from test_intelligence_pipeline import instrument, plan, portfolio

    from quant_ai.agents.swarm_runtime import SwarmPaperTradingService
    from quant_ai.execution.paper_ledger import PaperBrokerService
    from quant_ai.intelligence.pipeline import SwarmMarketAnalysisPipeline
    from quant_ai.intelligence.sandbox import (
        SandboxFundamentalDataProvider,
        SandboxMacroIndicatorProvider,
        SandboxNewsSentimentProvider,
    )
    from quant_ai.marketdata.feed import UsaSandboxMarketDataFeed
    broker = PaperBrokerService(tmp_path / "pipeline.sqlite", slippage_bps=D(0))
    runtime = SwarmPaperTradingService(broker=broker, oms=DurableOms(tmp_path / "pipeline-oms.sqlite"))
    pipeline = SwarmMarketAnalysisPipeline(UsaSandboxMarketDataFeed(), SandboxNewsSentimentProvider(),
        SandboxFundamentalDataProvider(), SandboxMacroIndicatorProvider(), runtime=runtime,
        bind_order_instruments=True)
    try:
        selected = instrument()
        result = pipeline.run(selected, NOW, plan(), portfolio(), quantity=10, country="USA", tenant_id="bound")
        assert isinstance(result.execution.proposal, InstrumentBoundTradeProposal)
        assert result.execution.proposal.instrument == selected
        assert result.execution.risk_decision.approved
        assert result.execution.risk_decision.order.instrument == selected
        assert result.execution.fill is not None
        raw = broker._connection.execute("SELECT instrument_identity FROM paper_ledger").fetchone()[0]
        assert raw == canonical_instrument_identity(selected)
    finally:
        runtime.oms.close()
        broker.close()


def test_bound_proposal_defensively_copies_metadata():
    metadata = {"source": "original"}
    selected = cash_instrument(metadata=metadata)
    p = InstrumentBoundTradeProposal(**vars(proposal()), instrument=selected)
    metadata["source"] = "changed"
    assert p.instrument.metadata["source"] == "original"
    with pytest.raises(TypeError, match="immutable"):
        p.instrument.metadata["source"] = "changed"


def test_replacement_preserves_contract_and_requires_new_risk_approval(tmp_path):
    with DurableOms(tmp_path / "replace.sqlite") as oms:
        order = bound_order()
        row = oms.create(order, decision_id="first", now=NOW)
        oms.cancel(row.client_order_id, reason="synthetic", now=NOW)
        child = oms.replace_cancelled(row.client_order_id, replace(order, reference_price=D(101)),
                                      decision_id="second", reason="repricing", now=NOW)
        assert child.instrument_identity == canonical_instrument_identity(order.instrument)
        assert child.state.value == "CREATED"
        assert oms.verify(child.client_order_id)["verified"]
        assert oms.verify(row.client_order_id)["verified"]


@pytest.mark.parametrize("change,code", [
    ({"expiry": date(2026, 9, 15)}, "contract_expired"),
    ({"expiry": date(2026, 9, 18)}, "contract_in_rollover"),
])
def test_warden_rejects_expired_or_rollover_entry_before_margin(change, code):
    from test_institutional_paper_coordinator import capital

    from quant_ai.domain.models import PortfolioSnapshot
    from quant_ai.risk.warden import RiskWarden
    instrument = future(**change)
    p = InstrumentBoundTradeProposal(**{**vars(proposal()), "symbol": instrument.symbol,
        "asset_class": instrument.asset_class}, instrument=instrument)
    result = RiskWarden().evaluate(p, capital(), PortfolioSnapshot(D(100000), D(0), D(0)), now=NOW)
    assert not result.approved
    assert code in result.reason


def test_rollover_covered_exit_keeps_identity_without_margin_source():
    from test_institutional_paper_coordinator import capital

    from quant_ai.domain.models import PortfolioSnapshot
    from quant_ai.risk.warden import RiskWarden
    instrument = future(expiry=date(2026, 9, 18))
    p = InstrumentBoundTradeProposal(**{**vars(proposal(side=Side.SELL)), "symbol": instrument.symbol,
        "asset_class": instrument.asset_class}, instrument=instrument)
    book = PortfolioSnapshot(D(100000), D(0), D(1000),
        symbol_quantity={instrument.symbol: 10}, symbol_exposure={instrument.symbol: D(1000)})
    result = RiskWarden().evaluate(p, capital(), book, now=NOW)
    assert result.approved
    assert result.order.instrument == instrument


def test_dated_instrument_is_serializable_in_governance_fingerprint(tmp_path):
    from quant_ai.execution.institutional import request_fingerprint
    h = Harness(tmp_path)
    try:
        selected = future()
        p = InstrumentBoundTradeProposal(**{**vars(proposal()), "symbol": selected.symbol,
            "asset_class": selected.asset_class}, instrument=selected)
        request = make_request(h.broker, p=p)
        digest = request_fingerprint(request)
        assert len(digest) == 64
        other = replace(request, proposal=replace(p, instrument=future(expiry=date(2026, 12, 6))))
        assert request_fingerprint(other) != digest
    finally:
        h.close()


def test_contract_currency_cannot_be_booked_in_different_currency(tmp_path):
    h = Harness(tmp_path)
    try:
        p = InstrumentBoundTradeProposal(**vars(proposal()), instrument=cash_instrument())
        with pytest.raises(ValueError, match="institutional_contract_currency_mismatch"):
            replace(make_request(h.broker, p=p), currency="USD")
    finally:
        h.close()


def test_child_approval_cannot_switch_contract_after_parent_approval(tmp_path, monkeypatch):
    from quant_ai.risk.warden import WardenDecision
    h = Harness(tmp_path)
    try:
        p = InstrumentBoundTradeProposal(**vars(proposal()), instrument=cash_instrument())
        request = make_request(h.broker, p=p)
        prepared = h.coordinator.prepare(request)
        foreign = replace(prepared.approved_order, instrument=cash_instrument(exchange="BSE"))
        monkeypatch.setattr(h.coordinator.warden, "evaluate", lambda *a, **k: WardenDecision(True, "test", foreign))
        result = h.coordinator.execute_due(prepared.program.program_id, now=NOW)
        assert result.reason == "execution_child_approval_identity_mismatch"
        assert h.broker.ledger_entries("tenant") == ()
        assert h.oms.all_orders("tenant") == ()
    finally:
        h.close()


def test_bound_program_resumes_exact_parent_without_replaying_first_slice(tmp_path):
    p = InstrumentBoundTradeProposal(**vars(proposal(quantity=20)), instrument=cash_instrument())
    h = Harness(tmp_path)
    request = make_request(h.broker, p=p, algorithm=ExecutionAlgorithm.TWAP,
        buckets=(VolumeBucket(NOW, 1000), VolumeBucket(NOW + timedelta(minutes=10), 1000)))
    try:
        prepared = h.coordinator.prepare(request)
        first = h.coordinator.execute_due(prepared.program.program_id, now=NOW)
        assert first.executed_sequences == (1,)
    finally:
        h.close()
    h = Harness(tmp_path)
    try:
        h.coordinator.bind_runtime_context(prepared.program.program_id, request=request,
                                          parent_order=prepared.approved_order)
        result = h.coordinator.execute_due(prepared.program.program_id, now=NOW + timedelta(minutes=10))
        assert result.executed_sequences == (2,)
        assert len(h.broker.ledger_entries("tenant")) == 2
        assert result.program.state.value == "COMPLETE"
        for item in result.program.slices:
            assert h.oms.get(item.client_order_id).instrument_identity == canonical_instrument_identity(p.instrument)
        assert h.journal.native_balance("tenant", "SECURITIES_COST", "INR") == D(2000)
    finally:
        h.close()


def test_oms_contract_projection_tamper_is_detected(tmp_path):
    with DurableOms(tmp_path / "tamper.sqlite") as oms:
        row = oms.create(bound_order(), decision_id="first", now=NOW)
        with oms.db:
            oms.db.execute("UPDATE oms_orders SET instrument_identity=? WHERE client_order_id=?",
                (canonical_instrument_identity(future(expiry=date(2026, 12, 6))), row.client_order_id))
        with pytest.raises(ValueError, match="oms_identity_projection_mismatch"):
            oms.verify(row.client_order_id)


def test_legacy_program_without_parent_snapshot_cannot_rebind_by_symbol(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    try:
        request = make_request(h.broker)
        create = h.programs.create
        def legacy_create(**kwargs):
            # Model the complete older INSERT shape, before parent/policy snapshots.
            from quant_ai.execution.institutional import request_fingerprint
            kwargs["parent_order_payload"] = None
            kwargs.pop("risk_authority_payload", None)
            kwargs["runtime_context_sha256"] = request_fingerprint(request)
            return create(**kwargs)
        monkeypatch.setattr(h.programs, "create", legacy_create)
        prepared = h.coordinator.prepare(request)
        with pytest.raises(ValueError, match="execution_program_runtime_context_mismatch"):
            h.coordinator.bind_runtime_context(prepared.program.program_id, request=request,
                                               parent_order=prepared.approved_order)
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        h.close()


def test_first_broker_observation_cannot_override_decision_exchange(tmp_path):
    from test_broker_lifecycle_reconciliation import NOW as CAPTURE_NOW
    from test_broker_lifecycle_reconciliation import order_capture

    from quant_ai.execution.broker_lifecycle import OmsBrokerLifecycleReconciler
    selected = Instrument("RELIANCE", Market.INDIA, AssetClass.EQUITY, "INR", "BSE")
    order = replace(bound_order(selected), quantity=3)
    capture = order_capture()
    with DurableOms(tmp_path / "first-binding.sqlite") as oms:
        row = oms.create(order, decision_id="bse", now=CAPTURE_NOW)
        oms.approve_risk(row.client_order_id, now=CAPTURE_NOW)
        oms.submission_uncertain(row.client_order_id, reason="synthetic", now=CAPTURE_NOW)
        before = tuple(oms.db.iterdump())
        result = OmsBrokerLifecycleReconciler().reconcile(oms, capture, tenant_id="tenant",
            account_ref=capture["accountRef"], bindings={row.client_order_id: "order-1"}, now=CAPTURE_NOW)
        assert result.status == "discrepancy"
        assert result.issues[0].code == "broker_order_identity_mismatch"
        assert tuple(oms.db.iterdump()) == before
