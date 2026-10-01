"""Synthetic certified transition; no provider calls or production migration."""
from decimal import Decimal

import pytest
from test_pilot_closure import INSTRUMENT

from quant_ai.domain.models import InstrumentBoundOrderIntent, Market, OrderIntent, Side
from quant_ai.execution.paper_ledger import PaperBrokerService


def synthetic_transition(tmp_path):
    broker = PaperBrokerService(tmp_path / 'synthetic.db', slippage_bps=Decimal(0))
    legacy = OrderIntent('INFY', Market.INDIA, Side.BUY, 2, Decimal(100),
                         'synthetic', tenant_id='pilot', stop_price=Decimal(95))
    broker.submit_with_evidence(legacy,
        {'schema': 'pramana.swarm_fill.v1', 'event_type': 'swarm_fill'}, 'synthetic-legacy')
    broker.close()
    path = tmp_path / 'synthetic.db'
    path.chmod(0o600)
    from quant_ai.operations.paper_identity_transition import (
        apply_reviewed_transition,
        inspect_oms,
        inspect_transition,
        provision_empty_oms,
    )
    from quant_ai.orders.oms import DurableOms
    oms_path = tmp_path / 'synthetic-oms.db'
    DurableOms(oms_path).close()
    provision_empty_oms(oms_path, paper_only=True,
        reviewed_empty_sha256=inspect_oms(oms_path)['empty_schema_sha256'])
    arguments = {'tenant': 'pilot', 'catalog': (INSTRUMENT,), 'account_binding': 'a'*64,
                 'catalog_capture_sha256': 'b'*64, 'paper_only': True}
    plan = inspect_transition(path, oms_path, **arguments)
    apply_reviewed_transition(path, oms_path, reviewed_plan_sha256=plan['sha256'], **arguments)
    broker = PaperBrokerService(path, slippage_bps=Decimal(0))
    return broker


def test_holding_only_transition_reconciles(tmp_path):
    broker = synthetic_transition(tmp_path)
    try:
        result = broker.reconcile('pilot')
        assert result['status'] == 'matched', result['issues']
    finally:
        broker.close()


@pytest.mark.parametrize('side,quantity,protective', [
    (Side.SELL, 1, True), (Side.SELL, 2, True),
    (Side.SELL, 1, False), (Side.SELL, 2, False), (Side.BUY, 1, False),
])
def test_first_bound_fill_has_valid_prior_history_receipt(tmp_path, side, quantity, protective):
    broker = synthetic_transition(tmp_path)
    try:
        order = InstrumentBoundOrderIntent('INFY', Market.INDIA, side, quantity, Decimal(100),
                    'synthetic', tenant_id='pilot', stop_price=Decimal(95), instrument=INSTRUMENT)
        if protective:
            result = broker.sell_protected(order, {'schema': 'pramana.protective_exit.v1', 'event_type': 'protective_exit'}, None)
            receipt = broker.protected_fill_receipt(result.order_id, 'pilot')
        else:
            result = broker.submit_with_evidence(order,
                {'schema': 'pramana.swarm_fill.v1', 'event_type': 'swarm_fill'}, 'synthetic-next')
            receipt = broker.submission_receipt('synthetic-next', 'pilot')
        assert receipt.entry.order_id == result.order_id
    finally:
        broker.close()


def test_oms_has_durable_instance_identity_before_new_fills(tmp_path):
    from quant_ai.operations.paper_identity_transition import inspect_oms, provision_empty_oms
    from quant_ai.orders.oms import DurableOms
    path = tmp_path / 'synthetic-oms.db'
    DurableOms(path).close()
    before = inspect_oms(path)
    value = provision_empty_oms(path, paper_only=True,
                              reviewed_empty_sha256=before['empty_schema_sha256'])
    assert inspect_oms(path)['instance_uuid'] == value


def test_certificate_tamper_holds_reconciliation_but_does_not_trap_exit(tmp_path):
    broker = synthetic_transition(tmp_path)
    try:
        # Synthetic tamper simulates lost/corrupt metadata; no production repair exists.
        broker._connection.execute('DROP TRIGGER paper_identity_transition_update_blocked')
        broker._connection.execute("UPDATE paper_identity_transitions SET sha256=?", ('f'*64,))
        broker._connection.commit()
        assert broker.reconcile('pilot')['status'] == 'mismatch'
        order = InstrumentBoundOrderIntent('INFY', Market.INDIA, Side.SELL, 2, Decimal(100),
            'synthetic', tenant_id='pilot', instrument=INSTRUMENT)
        result = broker.sell_protected(order,
            {'schema': 'pramana.protective_exit.v1', 'event_type': 'protective_exit'}, None)
        assert result.status == 'FILLED'
        assert broker._connection.execute('SELECT COUNT(*) FROM paper_positions WHERE tenant_id=?', ('pilot',)).fetchone()[0] == 0
    finally:
        broker.close()


def test_first_fill_then_restart_reconciles_without_legacy_rewrite(tmp_path):
    broker = synthetic_transition(tmp_path)
    original = dict(broker._connection.execute('SELECT * FROM paper_ledger ORDER BY id LIMIT 1').fetchone())
    order = InstrumentBoundOrderIntent('INFY', Market.INDIA, Side.BUY, 1, Decimal(100),
        'synthetic', tenant_id='pilot', stop_price=Decimal(95), instrument=INSTRUMENT)
    broker.submit_with_evidence(order,
        {'schema': 'pramana.swarm_fill.v1', 'event_type': 'swarm_fill'}, 'synthetic-next')
    broker.close()
    broker = PaperBrokerService(tmp_path / 'synthetic.db', slippage_bps=Decimal(0))
    try:
        assert broker.reconcile('pilot')['status'] == 'matched'
        assert dict(broker._connection.execute('SELECT * FROM paper_ledger ORDER BY id LIMIT 1').fetchone()) == original
        assert broker.submission_receipt('synthetic-next', 'pilot').entry.quantity == 1
    finally:
        broker.close()


def entry_issue_for(broker, oms):
    from types import SimpleNamespace

    from quant_ai.governance.runtime_identity import runtime_identity_entry_issue
    runtime = SimpleNamespace(broker=broker, oms=oms)
    daemon = SimpleNamespace(tenant_id='pilot', strategy_manifest=SimpleNamespace(summary={'status': 'matched'}),
        scheduler=SimpleNamespace(pipeline=SimpleNamespace(runtime=runtime, bind_order_instruments=True)))
    return runtime_identity_entry_issue(daemon)


def test_exact_legacy_cohort_is_not_fabricated_oms_and_new_fill_stays_strict(tmp_path):
    from quant_ai.orders.oms import DurableOms
    broker = synthetic_transition(tmp_path)
    oms = DurableOms(tmp_path / 'synthetic-oms.db')
    try:
        assert oms.all_orders('pilot') == () or not oms.all_orders('pilot')
        assert entry_issue_for(broker, oms) is None
        order = InstrumentBoundOrderIntent('INFY', Market.INDIA, Side.BUY, 1, Decimal(100),
            'synthetic', tenant_id='pilot', stop_price=Decimal(95), instrument=INSTRUMENT)
        broker.submit_with_evidence(order,
            {'schema': 'pramana.swarm_fill.v1', 'event_type': 'swarm_fill'}, 'synthetic-unlinked')
        assert entry_issue_for(broker, oms) == 'runtime_identity_oms_recovery_required'
    finally:
        broker.close()
        oms.close()


@pytest.mark.parametrize('protective', [False, True])
def test_same_path_empty_oms_replacement_is_blocked_before_swarm_fill(tmp_path, protective):
    from quant_ai.orders.oms import DurableOms
    broker = synthetic_transition(tmp_path)
    path = tmp_path / 'synthetic-oms.db'
    oms = DurableOms(path)
    if protective:
        order = InstrumentBoundOrderIntent('INFY', Market.INDIA, Side.SELL, 1, Decimal(100),
            'synthetic', tenant_id='pilot', instrument=INSTRUMENT)
        broker.sell_protected(order,
            {'schema': 'pramana.protective_exit.v1', 'event_type': 'protective_exit'}, None)
    assert entry_issue_for(broker, oms) is None
    oms.close()
    path.rename(tmp_path / 'old-instance.sqlite')
    replacement = DurableOms(path)
    try:
        assert entry_issue_for(broker, replacement) is not None
    finally:
        replacement.close()
        broker.close()


def test_low_id_and_backdated_new_fills_do_not_join_certified_cohort(tmp_path):
    from quant_ai.operations.paper_identity_transition import TransitionReplay
    broker = synthetic_transition(tmp_path)
    try:
        order = InstrumentBoundOrderIntent('INFY', Market.INDIA, Side.BUY, 1, Decimal(100),
            'synthetic', tenant_id='pilot', stop_price=Decimal(95), instrument=INSTRUMENT)
        result = broker.buy(order)
        # Deliberate corruption of a synthetic row, not a recovery operation.
        with broker._connection:
            broker._connection.execute('UPDATE paper_ledger SET id=0,created_at=? WHERE order_id=?',
                                       ('2000-01-01T00:00:00+00:00', result.order_id))
        with pytest.raises(ValueError, match='certificate_or_cohort_invalid'):
            TransitionReplay(broker._connection, 'pilot')
    finally:
        broker.close()


def test_old_receipt_remains_valid_and_protective_accounting_replays_boundary(tmp_path):
    from types import SimpleNamespace

    from quant_ai.accounting.protective import ProtectiveExitAccounting
    broker = synthetic_transition(tmp_path)
    try:
        assert broker.submission_receipt('synthetic-legacy', 'pilot').entry.instrument_identity is None
        order = InstrumentBoundOrderIntent('INFY', Market.INDIA, Side.SELL, 2, Decimal(94),
            'synthetic', tenant_id='pilot', instrument=INSTRUMENT)
        broker.sell_protected(order,
            {'schema': 'pramana.protective_exit.v1', 'event_type': 'protective_exit'}, None)
        assert broker.reconcile('pilot')['status'] == 'matched'
        accounting = SimpleNamespace(journal=SimpleNamespace(base_currency='INR'))
        adapter = ProtectiveExitAccounting(broker, accounting, currency='INR')
        batches = adapter._batches(broker.ledger_entries('pilot'), broker.cost_entries('pilot'))
        assert batches
        assert broker.submission_receipt('synthetic-legacy', 'pilot').entry.instrument_identity is None
    finally:
        broker.close()


def test_backdated_higher_id_fill_still_requires_real_oms_link(tmp_path):
    from quant_ai.orders.oms import DurableOms
    broker = synthetic_transition(tmp_path)
    oms = DurableOms(tmp_path / 'synthetic-oms.db')
    try:
        order = InstrumentBoundOrderIntent('INFY', Market.INDIA, Side.BUY, 1, Decimal(100),
            'synthetic', tenant_id='pilot', stop_price=Decimal(95), instrument=INSTRUMENT)
        result = broker.buy(order)
        with broker._connection:
            broker._connection.execute('UPDATE paper_ledger SET created_at=? WHERE order_id=?',
                ('2000-01-01T00:00:00+00:00', result.order_id))
        assert entry_issue_for(broker, oms) == 'runtime_identity_oms_recovery_required'
    finally:
        oms.close()
        broker.close()


def test_protective_outbox_hash_cannot_grant_exemption(tmp_path):
    from quant_ai.orders.oms import DurableOms
    broker = synthetic_transition(tmp_path)
    oms = DurableOms(tmp_path / 'synthetic-oms.db')
    try:
        order = InstrumentBoundOrderIntent('INFY', Market.INDIA, Side.SELL, 1, Decimal(94),
            'synthetic', tenant_id='pilot', instrument=INSTRUMENT)
        broker.sell_protected(order,
            {'schema': 'pramana.protective_exit.v1', 'event_type': 'protective_exit'}, None)
        assert entry_issue_for(broker, oms) is None
        with broker._connection:
            broker._connection.execute("DROP TRIGGER protective_outbox_update_blocked")
            broker._connection.execute("UPDATE paper_protective_fill_outbox SET receipt_sha256=?", ('e'*64,))
        assert entry_issue_for(broker, oms) is not None
    finally:
        oms.close()
        broker.close()
