"""Independent-connection races must not mix checked and unchecked capacity state."""
from __future__ import annotations

import sqlite3

import pytest
from test_institutional_paper_coordinator import Harness, make_request
from test_shared_risk_position_capacity import capacity, complete_entry
from test_shared_risk_reservations import enable

from quant_ai.execution import shared_risk_binding as binding
from quant_ai.execution.shared_risk import SharedRiskError


def test_external_position_change_after_reconcile_does_not_free_unchecked_capacity(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    other = sqlite3.connect(tmp_path / "paper.sqlite")
    try:
        enable(h)
        complete_entry(h)
        original = binding.reconcile_paper
        calls = []

        def interleave(db, tenant):
            result = original(db, tenant)
            assert result["status"] == "matched"
            with other:
                other.execute("DELETE FROM paper_positions WHERE tenant_id=?", (tenant,))
            calls.append(True)
            return result

        monkeypatch.setattr(binding, "reconcile_paper", interleave)
        report = capacity(h)
        assert calls == [True]
        assert report["effectiveReservedLoss"] == "50"
        assert report["openPositionRisk"] == "50"
        assert not h.broker._connection.in_transaction
        assert not h.programs.db.in_transaction
        monkeypatch.setattr(binding, "reconcile_paper", original)
        with pytest.raises(SharedRiskError, match="position_capacity_reconciliation_failed"):
            capacity(h)
        assert not h.broker._connection.in_transaction
        assert not h.programs.db.in_transaction
    finally:
        other.close()
        h.close()


def test_external_slice_change_after_binding_is_not_adopted_in_capacity(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    other = sqlite3.connect(tmp_path / "programs.sqlite")
    try:
        enable(h)
        prepared = h.coordinator.prepare(make_request(h.broker))
        assert prepared.approved
        original = binding._verify_binding_pair
        calls = []

        def interleave(ledger, journal, tenant, **kwargs):
            result = original(ledger, journal, tenant, **kwargs)
            with other:
                other.execute("UPDATE execution_program_slices SET state='FAILED',failure_reason='synthetic_pre_submit_refusal' WHERE program_id=?",
                              (prepared.program.program_id,))
                other.execute("UPDATE execution_programs SET state='FAILED' WHERE program_id=?",
                              (prepared.program.program_id,))
            calls.append(True)
            return result

        monkeypatch.setattr(binding, "_verify_binding_pair", interleave)
        report = capacity(h)
        assert calls == [True]
        assert report["effectiveReservedLoss"] == "50"
        assert report["pendingRisk"] == "50"
        assert not h.broker._connection.in_transaction
        assert not h.programs.db.in_transaction
    finally:
        other.close()
        h.close()


@pytest.mark.parametrize("fail", [False, True])
def test_snapshot_does_not_commit_or_rollback_callers_outer_transactions(tmp_path, fail):
    h = Harness(tmp_path)
    try:
        enable(h)
        complete_entry(h)
        h.broker._connection.execute("BEGIN")
        h.programs.db.execute("BEGIN")
        if fail:
            h.broker._connection.execute("UPDATE paper_accounts SET cash_balance='1' WHERE tenant_id='tenant'")
            with pytest.raises(SharedRiskError, match="position_capacity_reconciliation_failed"):
                capacity(h)
        else:
            assert capacity(h)["effectiveReservedLoss"] == "50"
        assert h.broker._connection.in_transaction
        assert h.programs.db.in_transaction
        h.broker._connection.rollback()
        h.programs.db.rollback()
        assert capacity(h)["effectiveReservedLoss"] == "50"
    finally:
        h.close()


def test_remaining_ambiguous_shares_keep_highest_parent_risk_first(tmp_path):
    from dataclasses import replace
    from decimal import Decimal

    from test_institutional_paper_coordinator import NOW, proposal
    from test_shared_risk_position_capacity import complete_exit, next_entry
    from test_shared_risk_reservations import policy

    h = Harness(tmp_path)
    try:
        enable(h)
        h.coordinator.shared_risk_policy = replace(policy(), max_loss_fraction=Decimal('.01'))
        complete_entry(h)
        request = next_entry(h, 'higher-risk-entry')
        request = replace(request, proposal=replace(proposal(decision_id='higher-risk-entry'), stop_price=Decimal(90)))
        prepared = h.coordinator.prepare(request)
        assert prepared.approved, prepared.reason
        result = h.coordinator.execute_due(prepared.program.program_id, now=NOW)
        assert result.stage.value == 'COMPLETE', result.reason
        complete_exit(h, quantity=15)
        report = capacity(h)
        assert report['rawReservedLoss'] == '150'
        assert report['openPositionRisk'] == '50'
        assert report['effectiveReservedLoss'] == '50'
        assert report['pendingRisk'] == '0'
    finally:
        h.close()


def test_two_independent_coordinators_cannot_spend_same_released_capacity(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from test_shared_risk_position_capacity import complete_exit, next_entry

    initial = Harness(tmp_path)
    try:
        enable(initial)
        complete_entry(initial)
        complete_exit(initial)
    finally:
        initial.close()
    peers = [Harness(tmp_path), Harness(tmp_path)]
    ready = Barrier(2, timeout=10)
    try:
        for peer in peers:
            enable(peer)

        def prepare(index):
            request = next_entry(peers[index], f'competing-replacement-{index}')
            ready.wait()
            return peers[index].coordinator.prepare(request)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(prepare, (0, 1)))
        assert sum(result.approved for result in results) == 1
        refused = next(result for result in results if not result.approved)
        assert refused.reason == 'shared_risk_budget_exceeded'
        assert capacity(peers[0])['effectiveReservedLoss'] == '50'
        assert capacity(peers[0]) == capacity(peers[1])
    finally:
        for peer in peers:
            peer.close()


@pytest.mark.parametrize('child,parent,reason,broker_id', [
    ('FAILED', 'FAILED', None, None),
    ('CANCELLED', 'CANCELLED', None, None),
    ('FAILED', 'ACTIVE', 'pre_submit_refused', None),
    ('FAILED', 'FAILED', '  ', None),
    ('FAILED', 'FAILED', 'pre_submit_refused', 'PAPER-no-receipt'),
    ('CANCELLED', 'FAILED', 'pre_submit_refused', None),
], ids=['missing_failure', 'missing_release', 'active_parent', 'blank_failure', 'missing_broker_receipt', 'cancelled_child'])
def test_unsubstantiated_terminal_label_does_not_free_capacity(tmp_path, child, parent, reason, broker_id):
    h = Harness(tmp_path)
    try:
        enable(h)
        prepared = h.coordinator.prepare(make_request(h.broker))
        assert prepared.approved
        pid = prepared.program.program_id
        with h.programs.db:
            h.programs.db.execute('UPDATE execution_programs SET state=? WHERE program_id=?', (parent, pid))
            h.programs.db.execute('UPDATE execution_program_slices SET state=?,failure_reason=?,broker_order_id=? WHERE program_id=?',
                                 (child, reason, broker_id, pid))
        with pytest.raises(SharedRiskError, match='position_capacity'):
            capacity(h)
    finally:
        h.close()
