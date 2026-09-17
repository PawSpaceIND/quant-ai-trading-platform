"""Synthetic arithmetic and authority tests; no forward trading edge is asserted."""
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

from quant_ai.decision.edge import CalibratedEdgeGate, EdgePolicy
from quant_ai.domain.models import Side
from quant_ai.execution.planner import ExecutionAlgorithm, VolumeBucket

D = Decimal


def test_kelly_uses_cost_adjusted_win_and_loss_economics():
    gate = CalibratedEdgeGate(EdgePolicy(
        kelly_fraction=D(1), max_risk_fraction=D(1), min_conservative_edge=D(0),
    ))
    result = gate.evaluate(evidence(
        calibrated_probability=D(".6"), calibration_error=D(0),
        probability_uncertainty=D(0), average_win_return=D(".02"),
        average_loss_return=D(".01"), expected_cost_return=D(".005"),
    ))
    assert result.approved
    assert result.raw_kelly_fraction == D(".2")
    assert result.recommended_risk_fraction == D(".2")


def test_cost_increase_reduces_uncapped_kelly_not_only_expectancy():
    gate = CalibratedEdgeGate(EdgePolicy(
        kelly_fraction=D(1), max_risk_fraction=D(1), min_conservative_edge=D(0),
    ))
    cheap = gate.evaluate(evidence(expected_cost_return=D(0)))
    costly = gate.evaluate(evidence(expected_cost_return=D(".01")))
    assert cheap.approved and costly.approved
    assert costly.raw_kelly_fraction < cheap.raw_kelly_fraction


@pytest.mark.parametrize("cost", [".04", ".05"])
def test_nonpositive_net_win_never_produces_positive_sizing(cost):
    result = CalibratedEdgeGate().evaluate(evidence(expected_cost_return=D(cost)))
    assert not result.approved
    assert result.raw_kelly_fraction == 0
    assert result.recommended_risk_fraction == 0


@pytest.mark.parametrize("cap,approved", [(".0005", True), (".000499", False)])
def test_parent_planned_loss_obeys_edge_budget_to_exact_boundary(tmp_path, cap, approved):
    h = Harness(tmp_path)
    try:
        h.coordinator.edge_gate = CalibratedEdgeGate(EdgePolicy(max_risk_fraction=D(cap)))
        result = h.coordinator.prepare(make_request(h.broker))
        assert result.approved is approved
        if not approved:
            assert result.reason == "edge_planned_risk_limit"
            assert h.broker.ledger_entries("tenant") == ()
            assert h.oms.all_orders("tenant") == ()
    finally:
        h.close()


def test_empirical_adverse_payoff_and_cost_are_not_ignored_for_tight_stop(tmp_path):
    h = Harness(tmp_path)
    try:
        h.coordinator.edge_gate = CalibratedEdgeGate(EdgePolicy(max_risk_fraction=D(".0005")))
        request = make_request(h.broker)
        observed = replace(request.edge_evidence, average_win_return=D(".20"),
                           average_loss_return=D(".08"), expected_cost_return=D(".005"))
        result = h.coordinator.prepare(replace(request, edge_evidence=observed))
        assert not result.approved and result.reason == "edge_planned_risk_limit"
    finally:
        h.close()


def test_due_slice_rechecks_edge_budget_against_current_equity(tmp_path):
    h = Harness(tmp_path)
    try:
        h.coordinator.edge_gate = CalibratedEdgeGate(EdgePolicy(max_risk_fraction=D(".0005")))
        prepared = h.coordinator.prepare(make_request(h.broker))
        assert prepared.approved
        original = h.coordinator.snapshot_provider
        h.coordinator.snapshot_provider = lambda: replace(original(), equity=D(99000))
        result = h.coordinator.execute_due(prepared.program.program_id, now=NOW)
        assert result.stage.value == "FAILED"
        assert result.reason == "edge_planned_risk_limit_at_slice"
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        h.close()


def test_fragmenting_parent_into_slices_does_not_multiply_edge_budget(tmp_path):
    h = Harness(tmp_path)
    try:
        h.coordinator.edge_gate = CalibratedEdgeGate(EdgePolicy(max_risk_fraction=D(".001")))
        request = make_request(h.broker, p=proposal(quantity=20), algorithm=ExecutionAlgorithm.TWAP,
            buckets=(VolumeBucket(NOW, 1000), VolumeBucket(NOW + timedelta(minutes=10), 1000)))
        prepared = h.coordinator.prepare(request)
        assert prepared.approved
        assert h.coordinator.execute_due(prepared.program.program_id, now=NOW).executed_sequences == (1,)
        original = h.coordinator.snapshot_provider
        h.coordinator.snapshot_provider = lambda: replace(original(), equity=D(99000))
        result = h.coordinator.execute_due(prepared.program.program_id, now=NOW + timedelta(minutes=10))
        assert result.stage.value == "FAILED"
        assert result.reason == "edge_planned_risk_limit_at_slice"
        assert len(h.broker.ledger_entries("tenant")) == 1
    finally:
        h.close()


@pytest.mark.parametrize("value", [D(-1), D("NaN"), D("Infinity")])
def test_invalid_strategy_exposure_is_refused_without_submission(tmp_path, value):
    h = Harness(tmp_path)
    try:
        prepared = h.coordinator.prepare(make_request(h.broker))
        h.coordinator.strategy_exposure_provider = lambda _: value
        result = h.coordinator.execute_due(prepared.program.program_id, now=NOW)
        assert result.stage.value == "FAILED"
        assert result.reason == "strategy_exposure_measure_unavailable"
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        h.close()


@pytest.mark.parametrize("defect", ["missing_holding", "quantity", "market_value", "extra_symbol"])
def test_factor_book_cannot_hide_or_relabel_projected_exposure(tmp_path, defect):
    h = Harness(tmp_path)
    try:
        request = make_request(h.broker)
        projected = request.projected_factor_positions
        if defect == "missing_holding":
            request = replace(request, portfolio=replace(request.portfolio, gross_exposure=D(1000),
                symbol_quantity={"TCS": 10}, symbol_exposure={"TCS": D(1000)}))
        elif defect == "quantity":
            projected = (replace(projected[0], quantity=1),)
        elif defect == "market_value":
            projected = factor_positions("100", 10)
        else:
            projected = (replace(projected[0], symbol="TCS"),)
        result = h.coordinator.prepare(replace(request, projected_factor_positions=projected))
        assert not result.approved
        assert result.reason.startswith("factor_book_projection_mismatch")
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        h.close()


def test_covered_exit_does_not_need_edge_or_factor_evidence(tmp_path):
    h = Harness(tmp_path)
    try:
        buy = h.coordinator.prepare(make_request(h.broker))
        assert h.coordinator.execute_due(buy.program.program_id, now=NOW).stage.value == "COMPLETE"
        request = make_request(h.broker, p=proposal(side=Side.SELL, decision_id="exit"))
        request = replace(request, edge_evidence=None, projected_factor_positions=())
        h.coordinator.edge_gate = CalibratedEdgeGate(EdgePolicy(max_risk_fraction=D(".000001")))
        sale = h.coordinator.prepare(request)
        assert sale.approved
        assert h.coordinator.execute_due(sale.program.program_id, now=NOW).stage.value == "COMPLETE"
        assert h.broker.get_positions("tenant") == ()
    finally:
        h.close()
