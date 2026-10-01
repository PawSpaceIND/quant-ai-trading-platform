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
    broker.configure_pilot((INSTRUMENT,), 'pilot')
    broker.close()
    initialize_runtime_schema(tmp_path, ledger, (INSTRUMENT,))
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


def test_malformed_named_oms_tables_are_not_a_supported_schema(tmp_path):
    from quant_ai.operations.paper_identity_transition import OMS_HISTORY
    from quant_ai.orders.oms import SCHEMA_VERSION
    path = tmp_path / 'malformed.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE oms_meta(id INTEGER,version INTEGER)')
        db.execute('INSERT INTO oms_meta VALUES(1,?)', (SCHEMA_VERSION,))
        for table in OMS_HISTORY:
            db.execute(f'CREATE TABLE {table}(wrong_column TEXT)')
    path.chmod(0o600)
    before = path.read_bytes()
    with pytest.raises(ValueError, match='supported_schema_required'):
        inspect_oms(path)
    assert path.read_bytes() == before


def test_supported_oms_requires_immutable_history_trigger(tmp_path):
    path = empty(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute('DROP TRIGGER oms_events_update_blocked')
    with pytest.raises(ValueError, match='supported_schema_required'):
        inspect_oms(path)


@pytest.mark.parametrize('mutation', ["UPDATE paper_accounts SET cash_balance='0'",
                                     'UPDATE paper_positions SET quantity=99',
                                     "UPDATE paper_cost_ledger SET amount='999'",
                                     "UPDATE paper_ledger SET fill_price='999'"])
def test_unreviewed_ledger_triggers_refuse_before_any_write(tmp_path, mutation):
    from quant_ai.operations.paper_identity_transition import apply_reviewed_transition
    ledger, oms, kwargs, inspect = legacy_plan_fixture(tmp_path)
    plan = inspect(ledger, oms, **kwargs)
    with sqlite3.connect(ledger) as db:
        db.execute(f'CREATE TRIGGER synthetic_mutation AFTER UPDATE OF instrument_identity ON paper_positions BEGIN {mutation}; END')
        before = tuple(db.iterdump())
    with pytest.raises(ValueError, match='unreviewed_ledger_trigger'):
        inspect(ledger, oms, **kwargs)
    with pytest.raises(ValueError, match='unreviewed_ledger_trigger'):
        apply_reviewed_transition(ledger, oms, reviewed_plan_sha256=plan['sha256'], **kwargs)
    with sqlite3.connect(ledger) as db:
        assert tuple(db.iterdump()) == before


@pytest.mark.parametrize('mutation', ["UPDATE paper_accounts SET cash_balance='0'",
                                     'UPDATE paper_positions SET quantity=99',
                                     "UPDATE paper_ledger SET fill_price='999'",
                                     "INSERT INTO paper_cost_ledger(order_id,tenant_id,code,amount,cash_debit,created_at) VALUES('synthetic-orphan','pilot','TEST','999',1,'2026-01-01')"])
def test_post_mutation_guard_rolls_back_even_if_trigger_admission_is_bypassed(tmp_path, monkeypatch, mutation):
    import quant_ai.operations.paper_identity_transition as transition
    ledger, oms, kwargs, inspect = legacy_plan_fixture(tmp_path)
    # Exercise the separate post-write guard, not just the trigger allowlist.
    with sqlite3.connect(ledger) as db:
        db.execute(f'CREATE TRIGGER synthetic_mutation AFTER UPDATE OF instrument_identity ON paper_positions BEGIN {mutation}; END')
        before = tuple(db.iterdump())
    monkeypatch.setattr(transition, 'ledger_schema', transition.schema_rows)
    plan = inspect(ledger, oms, **kwargs)
    with pytest.raises(ValueError, match='post_conservation_failed'):
        transition.apply_reviewed_transition(ledger, oms, reviewed_plan_sha256=plan['sha256'], **kwargs)
    with sqlite3.connect(ledger) as db:
        assert tuple(db.iterdump()) == before


def test_missing_pilot_schema_refuses_before_apply(tmp_path):
    from quant_ai.operations.paper_identity_transition import apply_reviewed_transition
    ledger, oms, kwargs, inspect = legacy_plan_fixture(tmp_path)
    plan = inspect(ledger, oms, **kwargs)
    with sqlite3.connect(ledger) as db:
        db.execute('DROP TABLE pilot_scope')
        before = tuple(db.iterdump())
    with pytest.raises(ValueError, match='pilot_schema_initialization_required'):
        inspect(ledger, oms, **kwargs)
    with pytest.raises(ValueError, match='pilot_schema_initialization_required'):
        apply_reviewed_transition(ledger, oms, reviewed_plan_sha256=plan['sha256'], **kwargs)
    with sqlite3.connect(ledger) as db:
        assert tuple(db.iterdump()) == before


def initialize_runtime_schema(tmp_path, ledger, catalog):
    """Actual synthetic legacy builder initializes schema; no stream start or orders."""
    from test_runtime_contract_mode import build, close

    from quant_ai.governance.directives import FounderDirectives
    runner = build(tmp_path, database=ledger, order_identity_mode='legacy_cash', oms_database=None,
        directives=FounderDirectives(watchlist=tuple(catalog)),
        zerodha_instrument_tokens=tuple(range(1, len(catalog)+1)),
        zerodha_symbol_by_token={n: i.symbol for n, i in enumerate(catalog, 1)})
    close(runner)


def test_named_but_incomplete_runtime_table_refuses_before_apply(tmp_path):
    from quant_ai.operations.paper_identity_transition import apply_reviewed_transition
    ledger, oms, kwargs, inspect = legacy_plan_fixture(tmp_path)
    plan = inspect(ledger, oms, **kwargs)
    with sqlite3.connect(ledger) as db:
        db.execute('DROP TABLE pilot_runtime')
        db.execute('CREATE TABLE pilot_runtime(wrong_column TEXT)')
        before = tuple(db.iterdump())
    with pytest.raises(ValueError, match='runtime_schema_initialization_required'):
        inspect(ledger, oms, **kwargs)
    with pytest.raises(ValueError, match='runtime_schema_initialization_required'):
        apply_reviewed_transition(ledger, oms, reviewed_plan_sha256=plan['sha256'], **kwargs)
    with sqlite3.connect(ledger) as db:
        assert tuple(db.iterdump()) == before
