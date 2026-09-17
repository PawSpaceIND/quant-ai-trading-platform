"""Synthetic paper-only regression checks for the four institutional risk repairs."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from test_calibrated_edge_gate import evidence
from test_institutional_paper_coordinator import (
    NOW,
    Harness,
    factor_positions,
    make_request,
    proposal,
)

from quant_ai.accounting.journal import TradingJournal
from quant_ai.accounting.trading import TradingAccounting
from quant_ai.decision.edge import CalibratedEdgeGate, EdgePolicy
from quant_ai.domain.models import Market, OrderIntent, Side
from quant_ai.execution.institutional import InstitutionalPaperCoordinator
from quant_ai.execution.planner import ExecutionAlgorithm, VolumeBucket
from quant_ai.execution.program import ExecutionProgramJournal
from quant_ai.orders.oms import DurableOms
from quant_ai.risk.warden import RiskWarden

D = Decimal


@pytest.mark.parametrize("cost", ["0", ".005", ".019", ".02", ".025"])
def test_kelly_agrees_with_after_cost_payoffs_including_no_net_win(cost):
    gate = CalibratedEdgeGate(EdgePolicy(kelly_fraction=D(1), max_risk_fraction=D(1),
                                       min_conservative_edge=D(0)))
    value = evidence(calibrated_probability=D(".6"), calibration_error=D(0),
        probability_uncertainty=D(0), average_win_return=D(".02"),
        average_loss_return=D(".01"), expected_cost_return=D(cost))
    actual = gate.evaluate(value)
    win, loss = D(".02") - D(cost), D(".01") + D(cost)
    expected = max(D(0), D(".6") - D(".4") * loss / win) if win > 0 else D(0)
    assert actual.raw_kelly_fraction == expected
    assert actual.recommended_risk_fraction == (expected if actual.approved else D(0))
    assert not actual.approved or actual.expected_after_cost_return > 0


@pytest.mark.parametrize("cap,approved", [(".00085", True), (".000849", False)])
def test_empirical_plus_cost_risk_exact_boundary(tmp_path, cap, approved):
    h = Harness(tmp_path)
    try:
        request = make_request(h.broker)
        request = replace(request, edge_evidence=replace(request.edge_evidence,
            average_win_return=D(".2"), average_loss_return=D(".08"),
            expected_cost_return=D(".005")))
        h.coordinator.edge_gate = CalibratedEdgeGate(EdgePolicy(max_risk_fraction=D(cap)))
        actual = h.coordinator.prepare(request)
        assert actual.approved is approved
        assert h.broker.ledger_entries("tenant") == ()
        if not approved:
            assert actual.reason == "edge_planned_risk_limit"
            assert h.oms.all_orders("tenant") == ()
    finally:
        h.close()


@pytest.mark.parametrize("value", [D(-1), D("NaN"), D("sNaN"), D("Infinity"),
                                   D("-Infinity"), None, 1, True, "100"])
@pytest.mark.parametrize("phase", ["prepare", "slice"])
def test_invalid_strategy_measure_refuses_before_any_broker_submission(tmp_path, value, phase):
    h = Harness(tmp_path)
    try:
        request = make_request(h.broker)
        if phase == "slice":
            prepared = h.coordinator.prepare(request)
            assert prepared.approved
        h.coordinator.strategy_exposure_provider = lambda _: value
        result = (h.coordinator.prepare(request) if phase == "prepare" else
                  h.coordinator.execute_due(prepared.program.program_id, now=NOW))
        assert result.reason == "strategy_exposure_measure_unavailable"
        assert h.broker.ledger_entries("tenant") == ()
        assert h.oms.all_orders("tenant") == ()
    finally:
        h.close()


@pytest.mark.parametrize("provider", ["strategy", "factor"])
def test_provider_exception_fails_without_submission(tmp_path, provider):
    h = Harness(tmp_path)
    try:
        prepared = h.coordinator.prepare(make_request(h.broker))
        def unavailable(*args):
            raise RuntimeError("synthetic provider outage")
        if provider == "strategy":
            h.coordinator.strategy_exposure_provider = unavailable
        else:
            h.coordinator.factor_position_provider = unavailable
        result = h.coordinator.execute_due(prepared.program.program_id, now=NOW)
        assert result.stage.value == "FAILED"
        assert result.reason == ("strategy_exposure_measure_unavailable" if provider == "strategy"
            else "factor_book_projection_mismatch:provider_unavailable")
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        h.close()


@pytest.mark.parametrize("defect", ["missing", "quantity", "value", "symbol", "duplicate", "invalid"])
def test_live_slice_factor_inputs_must_match_full_projection(tmp_path, defect):
    h = Harness(tmp_path)
    try:
        prepared = h.coordinator.prepare(make_request(h.broker))
        row = factor_positions()[0]
        rows = {"missing": (), "quantity": (replace(row, quantity=1),),
            "value": (replace(row, market_value=D(100)),),
            "symbol": (replace(row, symbol="TCS"),), "duplicate": (row, row),
            "invalid": None}[defect]
        h.coordinator.factor_position_provider = lambda *_: rows
        result = h.coordinator.execute_due(prepared.program.program_id, now=NOW)
        assert result.stage.value == "FAILED"
        assert result.reason.startswith("factor_book_projection_mismatch")
        assert h.broker.ledger_entries("tenant") == ()
        assert h.oms.all_orders("tenant") == ()
    finally:
        h.close()


@pytest.mark.parametrize("defect", ["gross", "coverage", "negative", "nonfinite", "fractional", "boolean"])
def test_inconsistent_authoritative_snapshot_cannot_feed_factor_calculation(tmp_path, defect):
    h = Harness(tmp_path)
    try:
        request = make_request(h.broker)
        changes = {
            "gross": {"gross_exposure": D(1)},
            "coverage": {"symbol_quantity": {"TCS": 1}},
            "negative": {"symbol_quantity": {"TCS": 1}, "symbol_exposure": {"TCS": D(-1)}},
            "nonfinite": {"symbol_quantity": {"TCS": 1}, "symbol_exposure": {"TCS": D("NaN")}},
            "fractional": {"symbol_quantity": {"TCS": D(".5")}, "symbol_exposure": {"TCS": D(50)}},
            "boolean": {"symbol_quantity": {"TCS": True}, "symbol_exposure": {"TCS": D(100)}},
        }[defect]
        result = h.coordinator.prepare(replace(request, portfolio=replace(request.portfolio, **changes)))
        assert not result.approved
        assert result.reason.startswith("factor_book_projection_mismatch")
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        h.close()


def test_real_existing_holding_requires_coverage_and_complete_book_still_passes(tmp_path):
    h = Harness(tmp_path)
    try:
        fill = h.broker.buy(OrderIntent("TCS", Market.INDIA, Side.BUY, 10, D(100),
            "synthetic-preexisting", tenant_id="tenant", stop_price=D(95)))
        recorded = h.broker.ledger_entries("tenant")[0]
        h.accounting.buy_security(f"trade:{fill.order_id}", currency="INR", notional=D(1000),
            base_notional=D(1000), reference=f"paper fill {fill.order_id} TCS", at=recorded.created_at)
        request = make_request(h.broker)
        missing = h.coordinator.prepare(request)
        assert not missing.approved and missing.reason.startswith("factor_book_projection_mismatch")
        tcs = replace(factor_positions()[0], symbol="TCS")
        request = replace(request, projected_factor_positions=(tcs, *request.projected_factor_positions))
        prepared = h.coordinator.prepare(request)
        assert prepared.approved
        h.coordinator.factor_position_provider = lambda order, snapshot: (tcs, *h.factor_provider(order, snapshot))
        result = h.coordinator.execute_due(prepared.program.program_id, now=NOW)
        assert result.stage.value == "COMPLETE"
        assert {p.symbol for p in h.broker.get_positions("tenant")} == {"TCS", "INFY"}
    finally:
        h.close()


def test_same_parent_budget_is_checked_after_restart_and_first_slice(tmp_path):
    h = Harness(tmp_path)
    restored = []
    try:
        gate = CalibratedEdgeGate(EdgePolicy(max_risk_fraction=D(".001")))
        h.coordinator.edge_gate = gate
        request = make_request(h.broker, p=proposal(quantity=20), algorithm=ExecutionAlgorithm.TWAP,
            buckets=(VolumeBucket(NOW, 1000), VolumeBucket(NOW + timedelta(minutes=10), 1000)))
        prepared = h.coordinator.prepare(request)
        assert prepared.approved
        assert h.coordinator.execute_due(prepared.program.program_id, now=NOW).executed_sequences == (1,)
        h.programs.close(); h.oms.close(); h.journal.close()
        oms = DurableOms(tmp_path / "oms.sqlite")
        programs = ExecutionProgramJournal(tmp_path / "programs.sqlite")
        journal = TradingJournal(tmp_path / "accounting.sqlite", base_currency="INR")
        restored.extend([oms, programs, journal])
        coordinator = InstitutionalPaperCoordinator(broker=h.broker, oms=oms, programs=programs,
            accounting=TradingAccounting(journal, "tenant"), warden=RiskWarden(), edge_gate=gate,
            snapshot_provider=lambda: replace(h.coordinator.snapshot_provider(), equity=D(99000)),
            factor_position_provider=h.factor_provider, strategy_exposure_provider=lambda _:h.strategy_exposure(),
            slice_volume_provider=lambda *_: 1000)
        coordinator.bind_runtime_context(prepared.program.program_id, request=request,
                                         parent_order=prepared.approved_order)
        result = coordinator.execute_due(prepared.program.program_id, now=NOW + timedelta(minutes=10))
        assert result.reason == "edge_planned_risk_limit_at_slice"
        assert len(h.broker.ledger_entries("tenant")) == 1
        assert programs.get(prepared.program.program_id).executed_quantity == 10
    finally:
        for store in restored:
            store.close()
        # sqlite close() is idempotent; preserve the existing broker cleanup.
        h.close()


def test_covered_exit_does_not_consult_broken_entry_measurements(tmp_path):
    h = Harness(tmp_path)
    try:
        entry = h.coordinator.prepare(make_request(h.broker))
        assert h.coordinator.execute_due(entry.program.program_id, now=NOW).stage.value == "COMPLETE"
        def forbidden(*args):
            pytest.fail("Covered exit must not need entry-only evidence")
        h.coordinator.strategy_exposure_provider = forbidden
        h.coordinator.factor_position_provider = forbidden
        request = replace(make_request(h.broker,p=proposal(side=Side.SELL,decision_id="close")),
                          edge_evidence=None,projected_factor_positions=())
        exit_ = h.coordinator.prepare(request)
        assert exit_.approved
        assert h.coordinator.execute_due(exit_.program.program_id, now=NOW).stage.value == "COMPLETE"
        assert h.broker.get_positions("tenant") == ()
    finally:
        h.close()
