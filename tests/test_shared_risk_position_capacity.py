"""Position-linked shared-risk capacity uses reconciled broker state, never guessed release."""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from test_institutional_paper_coordinator import (
    NOW,
    FailOnceAccounting,
    Harness,
    make_request,
    proposal,
)
from test_shared_risk_reservations import enable, policy

from quant_ai.domain.models import Side
from quant_ai.execution.planner import ExecutionAlgorithm, VolumeBucket
from quant_ai.execution.shared_risk import SharedRiskError, verify_shared_risk
from quant_ai.execution.shared_risk_binding import position_linked_capacity

D = Decimal


def next_entry(h, decision_id):
    request = make_request(h.broker, p=proposal(decision_id=decision_id))
    return replace(
        request,
        projected_factor_positions=h.factor_provider(request.proposal, request.portfolio),
    )


def close_request(h, *, quantity=10, decision_id="close-position"):
    request = make_request(
        h.broker,
        p=proposal(quantity=quantity, side=Side.SELL, decision_id=decision_id),
    )
    return replace(
        request,
        edge_evidence=None,
        strategy_opportunities=(),
        strategy_correlations={},
        projected_factor_positions=(),
    )


def capacity(h):
    return position_linked_capacity(
        h.broker._connection, h.programs.db, "tenant"
    )


def complete_entry(h, *, decision_id="decision-1"):
    entry = h.coordinator.prepare(
        make_request(h.broker, p=proposal(decision_id=decision_id))
    )
    assert entry.approved
    result = h.coordinator.execute_due(entry.program.program_id, now=NOW)
    assert result.stage.value == "COMPLETE"
    return entry


def complete_exit(h, *, quantity=10, decision_id="close-position"):
    exit_program = h.coordinator.prepare(
        close_request(h, quantity=quantity, decision_id=decision_id)
    )
    assert exit_program.approved
    result = h.coordinator.execute_due(exit_program.program.program_id, now=NOW)
    assert result.stage.value == "COMPLETE"
    return exit_program


def test_reconciled_full_close_reopens_shared_account_capacity(tmp_path):
    h = Harness(tmp_path)
    try:
        enable(h)
        complete_entry(h)
        complete_exit(h)
        assert h.broker.get_positions("tenant") == ()
        assert h.broker.reconcile("tenant")["status"] == "matched"

        report = capacity(h)
        assert report["rawReservedLoss"] == "50"
        assert report["effectiveReservedLoss"] == "0"
        assert report["pendingRisk"] == "0"
        assert report["openPositionRisk"] == "0"
        assert report["activationAuthorized"] is False

        replacement = h.coordinator.prepare(next_entry(h, "after-close"))
        assert replacement.approved, replacement.reason
        executed = h.coordinator.execute_due(replacement.program.program_id, now=NOW)
        assert executed.stage.value == "COMPLETE", executed.reason
    finally:
        h.close()


def test_open_position_keeps_full_effective_charge(tmp_path):
    h = Harness(tmp_path)
    try:
        enable(h)
        complete_entry(h)
        report = capacity(h)
        assert report["rawReservedLoss"] == "50"
        assert report["effectiveReservedLoss"] == "50"
        assert report["openPositionRisk"] == "50"
        assert report["pendingRisk"] == "0"
        second = h.coordinator.prepare(next_entry(h, "while-open"))
        assert not second.approved and second.reason == "shared_risk_budget_exceeded"
    finally:
        h.close()


def test_partial_exit_reduces_only_reconciled_open_position_risk(tmp_path):
    h = Harness(tmp_path)
    try:
        enable(h)
        complete_entry(h)
        complete_exit(h, quantity=5, decision_id="partial-close")
        positions = h.broker.get_positions("tenant")
        assert len(positions) == 1 and positions[0].quantity == 5
        report = capacity(h)
        assert report["rawReservedLoss"] == "50"
        assert report["effectiveReservedLoss"] == "25"
        assert report["openPositionRisk"] == "25"
        assert report["pendingRisk"] == "0"
    finally:
        h.close()


def test_active_multislice_program_keeps_pending_plus_open_risk(tmp_path):
    h = Harness(tmp_path)
    try:
        enable(h)
        request = make_request(
            h.broker,
            p=proposal(quantity=10, decision_id="twap-active"),
            algorithm=ExecutionAlgorithm.TWAP,
            buckets=(
                VolumeBucket(NOW, 1000),
                VolumeBucket(NOW + timedelta(minutes=5), 1000),
            ),
        )
        prepared = h.coordinator.prepare(request)
        assert prepared.approved
        first = h.coordinator.execute_due(prepared.program.program_id, now=NOW)
        assert first.executed_sequences == (1,)
        report = capacity(h)
        assert report["rawReservedLoss"] == "50"
        assert report["effectiveReservedLoss"] == "50"
        assert report["openPositionRisk"] == "25"
        assert report["pendingRisk"] == "25"
    finally:
        h.close()


def test_failed_remaining_slice_releases_only_unused_parent_capacity(tmp_path):
    h = Harness(tmp_path)
    try:
        enable(h)
        h.coordinator.shared_risk_policy = replace(
            policy(), max_loss_fraction=D(".001")
        )
        request = make_request(
            h.broker,
            p=proposal(quantity=10, decision_id="twap-fails"),
            algorithm=ExecutionAlgorithm.TWAP,
            buckets=(
                VolumeBucket(NOW, 1000),
                VolumeBucket(NOW + timedelta(minutes=5), 1000),
            ),
        )
        prepared = h.coordinator.prepare(request)
        assert prepared.approved
        first = h.coordinator.execute_due(prepared.program.program_id, now=NOW)
        assert first.executed_sequences == (1,)
        h.daily_total_pnl = D("-2000")
        second = h.coordinator.execute_due(
            prepared.program.program_id, now=NOW + timedelta(minutes=5)
        )
        assert second.stage.value == "FAILED"
        report = capacity(h)
        assert report["rawReservedLoss"] == "50"
        assert report["effectiveReservedLoss"] == "25"
        assert report["openPositionRisk"] == "25"
        assert report["pendingRisk"] == "0"

        # Under the 100-unit account cap, the unused 25 is genuinely available,
        # but the still-open 5-share position remains charged.
        replacement = h.coordinator.prepare(next_entry(h, "after-partial-failure"))
        assert replacement.approved, replacement.reason
    finally:
        h.close()


def test_claimed_slice_without_broker_receipt_stays_fully_charged(tmp_path):
    h = Harness(tmp_path)
    try:
        enable(h)
        prepared = h.coordinator.prepare(make_request(h.broker))
        assert prepared.approved
        assert h.programs.claim_slice(
            prepared.program.program_id,
            1,
            client_order_id="synthetic-uncertain-child",
        )
        report = capacity(h)
        assert report["rawReservedLoss"] == "50"
        assert report["effectiveReservedLoss"] == "50"
        assert report["pendingRisk"] == "50"
        assert report["openPositionRisk"] == "0"
    finally:
        h.close()


def test_filled_unaccounted_position_stays_charged(tmp_path):
    h = Harness(tmp_path, accounting_cls=FailOnceAccounting)
    try:
        enable(h)
        prepared = h.coordinator.prepare(make_request(h.broker))
        assert prepared.approved
        result = h.coordinator.execute_due(prepared.program.program_id, now=NOW)
        assert result.stage.value == "FAILED"
        assert result.program.slices[0].state.value == "FILLED_UNACCOUNTED"
        report = capacity(h)
        assert report["effectiveReservedLoss"] == "50"
        assert report["openPositionRisk"] == "50"
        assert report["pendingRisk"] == "0"
    finally:
        h.close()


def test_never_claimed_cancellation_still_uses_existing_release_record(tmp_path):
    h = Harness(tmp_path)
    try:
        enable(h)
        prepared = h.coordinator.prepare(make_request(h.broker))
        assert prepared.approved
        h.coordinator.shared_risk.cancel_unclaimed(
            prepared.program.program_id, tenant_id="tenant", at=NOW
        )
        historical = verify_shared_risk(h.programs.db, "tenant")
        assert historical["reservedLoss"] == "0"
        report = capacity(h)
        assert report["rawReservedLoss"] == "0"
        assert report["effectiveReservedLoss"] == "0"
    finally:
        h.close()


def test_capacity_read_is_nonmutating_and_restart_deterministic(tmp_path):
    h = Harness(tmp_path)
    try:
        enable(h)
        complete_entry(h)
        complete_exit(h)
        before = (
            tuple(h.broker._connection.iterdump()),
            tuple(h.programs.db.iterdump()),
        )
        first = capacity(h)
        second = capacity(h)
        assert first == second
        assert (
            tuple(h.broker._connection.iterdump()),
            tuple(h.programs.db.iterdump()),
        ) == before
    finally:
        h.close()

    restarted = Harness(tmp_path)
    try:
        enable(restarted)
        report = capacity(restarted)
        assert report["rawReservedLoss"] == "50"
        assert report["effectiveReservedLoss"] == "0"
        replacement = restarted.coordinator.prepare(
            next_entry(restarted, "after-restart")
        )
        assert replacement.approved, replacement.reason
    finally:
        restarted.close()


def test_reconciliation_mismatch_refuses_to_free_capacity(tmp_path):
    h = Harness(tmp_path)
    try:
        enable(h)
        complete_entry(h)
        complete_exit(h)
        replacement_request = next_entry(h, "after-corruption")
        h.broker._connection.execute(
            "UPDATE paper_accounts SET cash_balance='1' WHERE tenant_id='tenant'"
        )
        h.broker._connection.commit()
        with pytest.raises(
            SharedRiskError, match="shared_risk_broker_position_capacity_reconciliation_failed"
        ):
            capacity(h)
        replacement = h.coordinator.prepare(replacement_request)
        assert not replacement.approved
        assert "reconciliation" in replacement.reason
    finally:
        h.close()



def test_prepare_refuses_impossible_effective_capacity_override(tmp_path, monkeypatch):
    from quant_ai.execution import institutional

    h = Harness(tmp_path)
    try:
        enable(h)
        prepared = h.coordinator.prepare(make_request(h.broker))
        assert prepared.approved
        monkeypatch.setattr(
            institutional,
            "position_linked_capacity",
            lambda *args, **kwargs: {"effectiveReservedLoss": "999"},
        )
        result = h.coordinator.prepare(next_entry(h, "impossible-prepare-capacity"))
        assert not result.approved
        assert result.reason == "shared_risk_position_capacity_exceeds_reservations"
    finally:
        h.close()


def test_dispatch_refuses_impossible_effective_capacity_override(tmp_path, monkeypatch):
    from quant_ai.execution import institutional

    h = Harness(tmp_path)
    try:
        enable(h)
        prepared = h.coordinator.prepare(make_request(h.broker))
        assert prepared.approved
        monkeypatch.setattr(
            institutional,
            "position_linked_capacity",
            lambda *args, **kwargs: {"effectiveReservedLoss": "999"},
        )
        result = h.coordinator.execute_due(prepared.program.program_id, now=NOW)
        assert result.stage.value == "FAILED"
        assert result.reason == "shared_risk_position_capacity_exceeds_reservations"
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        h.close()



def test_unselected_capacity_requires_broker_journal_binding(tmp_path):
    h = Harness(tmp_path)
    try:
        with pytest.raises(
            SharedRiskError, match="shared_risk_broker_position_capacity_binding_missing"
        ):
            capacity(h)
    finally:
        h.close()


def test_executed_slice_without_broker_fill_is_never_treated_as_released(tmp_path):
    h = Harness(tmp_path)
    try:
        enable(h)
        prepared = h.coordinator.prepare(make_request(h.broker))
        assert prepared.approved
        h.programs.db.execute(
            "UPDATE execution_program_slices SET state='EXECUTED' WHERE program_id=?",
            (prepared.program.program_id,),
        )
        h.programs.db.commit()
        with pytest.raises(
            SharedRiskError,
            match="shared_risk_broker_position_capacity_unrecorded_fill_state_invalid",
        ):
            capacity(h)
    finally:
        h.close()


def test_effective_capacity_can_never_exceed_validated_raw_reservations(
    tmp_path, monkeypatch
):
    from quant_ai.execution import shared_risk_binding

    h = Harness(tmp_path)
    try:
        enable(h)
        complete_entry(h)
        original = shared_risk_binding.verify_shared_risk

        def understated(*args, **kwargs):
            report = dict(original(*args, **kwargs))
            report["reservedLoss"] = "1"
            return report

        monkeypatch.setattr(shared_risk_binding, "verify_shared_risk", understated)
        with pytest.raises(
            SharedRiskError,
            match="shared_risk_broker_position_capacity_exceeds_reservations",
        ):
            capacity(h)
    finally:
        h.close()



def test_independent_protective_exit_reopens_capacity_without_order_replay(tmp_path):
    from quant_ai.execution.protective_exits import ProtectiveExitEngine

    h = Harness(tmp_path)
    try:
        enable(h)
        complete_entry(h)
        outcomes = ProtectiveExitEngine(
            h.broker, lambda _symbol: D(90), tenant_id="tenant"
        ).evaluate()
        assert len(outcomes) == 1 and outcomes[0].filled
        assert h.broker.get_positions("tenant") == ()
        # The shared-risk charge follows reconciled broker exposure. Accounting is
        # independently mirrored by the existing pre-submit/recovery control.
        assert capacity(h)["effectiveReservedLoss"] == "0"
        accounting = h.coordinator.reconcile_protective_accounting(currency="INR")
        assert accounting.status == "matched"

        replacement = h.coordinator.prepare(next_entry(h, "after-protective-exit"))
        assert replacement.approved, replacement.reason
        result = h.coordinator.execute_due(replacement.program.program_id, now=NOW)
        assert result.stage.value == "COMPLETE", result.reason
        assert len(h.broker.ledger_entries("tenant")) == 3
    finally:
        h.close()
