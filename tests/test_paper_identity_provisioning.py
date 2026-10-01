"""Synthetic OMS provisioning; no host migration or broker calls."""
import sqlite3

import pytest

from quant_ai.operations.paper_identity_transition import inspect_oms, provision_empty_oms
from quant_ai.orders.oms import DurableOms


def empty(tmp_path):
    path = tmp_path / 'synthetic.sqlite'
    DurableOms(path).close()
    return path


def test_inspection_is_read_only_and_explicit_provision_is_idempotent(tmp_path):
    path = empty(tmp_path)
    before = path.read_bytes()
    plan = inspect_oms(path)
    assert plan['instance_uuid'] is None
    assert path.read_bytes() == before
    first = provision_empty_oms(path, paper_only=True,
                               reviewed_empty_sha256=plan['empty_schema_sha256'])
    assert inspect_oms(path)['instance_uuid'] == first
    assert provision_empty_oms(path, paper_only=True,
                              reviewed_empty_sha256=plan['empty_schema_sha256']) == first
    with sqlite3.connect(path) as db:
        for sql in ("DELETE FROM pramana_oms_instance", "UPDATE pramana_oms_instance SET instance_uuid='changed'"):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(sql)


def test_replacement_same_path_changes_identity_before_any_fill(tmp_path):
    path = empty(tmp_path)
    plan = inspect_oms(path)
    first = provision_empty_oms(path, paper_only=True, reviewed_empty_sha256=plan['empty_schema_sha256'])
    # Synthetic replacement only; production flow does not delete databases.
    path.rename(tmp_path / 'original.sqlite')
    DurableOms(path).close()
    assert inspect_oms(path)['instance_uuid'] is None
    second = provision_empty_oms(path, paper_only=True, reviewed_empty_sha256=plan['empty_schema_sha256'])
    assert first != second


@pytest.mark.parametrize('flag', [False, None, 1, 'true'])
def test_nonpaper_provision_refuses_without_mutation(tmp_path, flag):
    path = empty(tmp_path)
    before = path.read_bytes()
    with pytest.raises(ValueError, match='paper_only'):
        provision_empty_oms(path, paper_only=flag, reviewed_empty_sha256='a'*64)
    assert path.read_bytes() == before


def test_changed_plan_and_orphan_history_refuse(tmp_path):
    path = empty(tmp_path)
    with pytest.raises(ValueError, match='plan_changed'):
        provision_empty_oms(path, paper_only=True, reviewed_empty_sha256='a'*64)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO oms_replacements VALUES('orphan','foreign','synthetic','2026-01-01')")
    with pytest.raises(ValueError, match='history_not_empty'):
        inspect_oms(path)


def test_missing_path_is_not_created(tmp_path):
    path = tmp_path / 'absent.sqlite'
    with pytest.raises(ValueError, match='private_regular'):
        inspect_oms(path)
    assert not path.exists()


def legacy_plan_fixture(tmp_path):
    from decimal import Decimal

    from test_pilot_closure import INSTRUMENT

    from quant_ai.domain.models import Market, OrderIntent, Side
    from quant_ai.execution.paper_ledger import PaperBrokerService
    from quant_ai.operations.paper_identity_transition import inspect_transition
    ledger = tmp_path / 'paper.sqlite'
    broker = PaperBrokerService(ledger, slippage_bps=Decimal(0))
    broker.buy(OrderIntent('INFY', Market.INDIA, Side.BUY, 2, Decimal(100),
                          'synthetic', tenant_id='pilot', stop_price=Decimal(95)))
    broker.close()
    ledger.chmod(0o600)
    oms = empty(tmp_path)
    initial = inspect_oms(oms)
    provision_empty_oms(oms, paper_only=True, reviewed_empty_sha256=initial['empty_schema_sha256'])
    kwargs = {'tenant': 'pilot', 'catalog': (INSTRUMENT,), 'account_binding': 'a'*64,
              'catalog_capture_sha256': 'b'*64, 'paper_only': True}
    return ledger, oms, kwargs, inspect_transition


def test_transition_planner_preserves_original_legacy_inventory(tmp_path):
    ledger, oms, kwargs, inspect = legacy_plan_fixture(tmp_path)
    before = (ledger.read_bytes(), oms.read_bytes())
    first = inspect(ledger, oms, **kwargs)
    assert inspect(ledger, oms, **kwargs) == first
    assert (ledger.read_bytes(), oms.read_bytes()) == before
    assert first['plan']['apply_supported'] is False
    assert first['plan']['legacy_inventory']['paper_ledger'][0]['instrument_identity'] is None
    assert first['plan']['legacy_inventory']['paper_positions'][0]['instrument_identity'] is None


def test_transition_planner_rejects_physical_zero_rows(tmp_path):
    ledger, oms, kwargs, inspect = legacy_plan_fixture(tmp_path)
    with sqlite3.connect(ledger) as db:
        db.execute('UPDATE paper_positions SET quantity=0')
    with pytest.raises(ValueError, match='physical_zero'):
        inspect(ledger, oms, **kwargs)


def test_transition_planner_requires_review_binding_and_catalog(tmp_path):
    from dataclasses import replace
    ledger, oms, kwargs, inspect = legacy_plan_fixture(tmp_path)
    with pytest.raises(ValueError, match='review_binding'):
        inspect(ledger, oms, **{**kwargs, 'account_binding': None})
    with pytest.raises(ValueError, match='held_identity_ambiguous'):
        inspect(ledger, oms, **{**kwargs, 'catalog': (replace(kwargs['catalog'][0], symbol='OTHER'),)})


def test_apply_failure_inside_transaction_leaves_no_partial_binding(tmp_path, monkeypatch):
    import quant_ai.operations.paper_identity_transition as transition
    ledger, oms, kwargs, inspect = legacy_plan_fixture(tmp_path)
    plan = inspect(ledger, oms, **kwargs)
    with sqlite3.connect(ledger) as db:
        before = tuple(db.iterdump())
    original = transition.digest
    def fail_pin(value):
        if value.get('schema') == 'pramana.runtime_order_identity.v1':
            raise RuntimeError('synthetic transaction interruption')
        return original(value)
    monkeypatch.setattr(transition, 'digest', fail_pin)
    with pytest.raises(RuntimeError, match='interruption'):
        transition.apply_reviewed_transition(ledger, oms,
            reviewed_plan_sha256=plan['sha256'], **kwargs)
    with sqlite3.connect(ledger) as db:
        assert tuple(db.iterdump()) == before


def test_apply_revalidates_plan_and_repeated_same_apply_is_idempotent(tmp_path):
    from quant_ai.operations.paper_identity_transition import apply_reviewed_transition
    ledger, oms, kwargs, inspect = legacy_plan_fixture(tmp_path)
    plan = inspect(ledger, oms, **kwargs)
    with pytest.raises(ValueError, match='plan_changed'):
        apply_reviewed_transition(ledger, oms, reviewed_plan_sha256='f'*64, **kwargs)
    assert apply_reviewed_transition(ledger, oms, reviewed_plan_sha256=plan['sha256'], **kwargs) == plan['sha256']
    with sqlite3.connect(ledger) as db:
        before = tuple(db.iterdump())
    assert apply_reviewed_transition(ledger, oms, reviewed_plan_sha256=plan['sha256'], **kwargs) == plan['sha256']
    with sqlite3.connect(ledger) as db:
        assert tuple(db.iterdump()) == before
    with pytest.raises(ValueError, match='reapply_conflict'):
        apply_reviewed_transition(ledger, oms, reviewed_plan_sha256=plan['sha256'],
                                 **{**kwargs, 'account_binding': 'c'*64})


@pytest.mark.parametrize('table', ['oms_orders', 'oms_events', 'oms_fills',
                                   'oms_broker_evidence_bindings', 'oms_replacements'])
def test_each_global_raw_history_table_blocks_provisioning(tmp_path, table):
    path = empty(tmp_path)
    with sqlite3.connect(path) as db:
        columns = list(db.execute(f'PRAGMA table_info({table})'))
        names = [r[1] for r in columns]
        values = [1 if r[2] == 'INTEGER' else 'synthetic-orphan' for r in columns]
        marks = ','.join('?' for _ in names)
        db.execute(f"INSERT INTO {table}({','.join(names)}) VALUES({marks})", values)
    with pytest.raises(ValueError, match='history_not_empty'):
        inspect_oms(path)


def test_actual_revalidation_refuses_changed_cash_without_identity_write(tmp_path):
    from quant_ai.operations.paper_identity_transition import apply_reviewed_transition
    ledger, oms, kwargs, inspect = legacy_plan_fixture(tmp_path)
    plan = inspect(ledger, oms, **kwargs)
    with sqlite3.connect(ledger) as db:
        db.execute("UPDATE paper_accounts SET cash_balance='1'")
        before = tuple(db.iterdump())
    with pytest.raises(ValueError, match='reconciliation_failed'):
        apply_reviewed_transition(ledger, oms, reviewed_plan_sha256=plan['sha256'], **kwargs)
    with sqlite3.connect(ledger) as db:
        assert tuple(db.iterdump()) == before
