"""Local paper identity transition primitives; no provider or runtime activation.

Provisioning is an explicit database write. Inspection never creates files or schema.
This module is not yet a complete migration/apply interface.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

OMS_HISTORY = ('oms_orders', 'oms_events', 'oms_fills',
               'oms_broker_evidence_bindings', 'oms_replacements')
INSTANCE_TABLE = 'pramana_oms_instance'


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _private_existing(path):
    path = Path(path)
    if (path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1
            or path.stat().st_mode & 0o077):
        raise ValueError('transition_private_regular_database_required')
    return path


@contextmanager
def read_database(path):
    """WAL-aware read transaction, never a store constructor or immutable=1."""
    path = _private_existing(path)
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('PRAGMA trusted_schema=OFF')
        db.execute('BEGIN')
        yield db
    finally:
        db.close()


def _tables(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def require_empty_oms(db):
    tables = _tables(db)
    if (not set(OMS_HISTORY) <= tables or 'oms_meta' not in tables
            or tables - {*OMS_HISTORY, 'oms_meta', INSTANCE_TABLE, 'sqlite_sequence'}):
        raise ValueError('transition_oms_schema_missing')
    meta = db.execute('SELECT id,version FROM oms_meta').fetchall()
    from quant_ai.orders.oms import PAPER_RECOVERY_SCHEMA_VERSION, SCHEMA_VERSION
    if len(meta) != 1 or meta[0]['id'] != 1 or meta[0]['version'] not in (SCHEMA_VERSION, PAPER_RECOVERY_SCHEMA_VERSION):
        raise ValueError('transition_oms_schema_invalid')
    # Global raw history, not all_orders(tenant): orphans/foreign tenants also refuse.
    for table in OMS_HISTORY:
        if db.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone():
            raise ValueError('transition_oms_history_not_empty')


def oms_instance(db):
    if INSTANCE_TABLE not in _tables(db):
        raise ValueError('transition_oms_instance_missing')
    rows = db.execute(f'SELECT singleton,instance_uuid FROM {INSTANCE_TABLE}').fetchall()
    if len(rows) != 1 or rows[0]['singleton'] != 1:
        raise ValueError('transition_oms_instance_invalid')
    value = rows[0]['instance_uuid']
    try:
        parsed = UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise ValueError('transition_oms_instance_invalid') from None
    if str(parsed) != value or parsed.version != 4:
        raise ValueError('transition_oms_instance_invalid')
    return value


def provision_empty_oms(path, *, paper_only, reviewed_empty_sha256):
    """Explicit internal identity provisioning; no broker credentials or constructor.

    Caller must quiesce writers. Compare an exact inspected empty schema fingerprint;
    rollback is not provided. A repeated identical provision preserves the UUID.
    """
    if paper_only is not True:
        raise ValueError('transition_paper_only_required')
    path = _private_existing(path)
    db = sqlite3.connect(path, timeout=2)
    db.row_factory = sqlite3.Row
    try:
        db.execute('BEGIN IMMEDIATE')
        require_empty_oms(db)
        if empty_oms_fingerprint(db) != reviewed_empty_sha256:
            raise ValueError('transition_oms_plan_changed')
        if INSTANCE_TABLE in _tables(db):
            value = oms_instance(db)
        else:
            value = str(uuid4())
            db.execute(f'CREATE TABLE {INSTANCE_TABLE}(singleton INTEGER PRIMARY KEY CHECK(singleton=1),instance_uuid TEXT NOT NULL UNIQUE)')
            db.execute(f'INSERT INTO {INSTANCE_TABLE} VALUES(1,?)', (value,))
            for verb in ('UPDATE', 'DELETE'):
                db.execute(f"CREATE TRIGGER oms_instance_{verb.lower()}_blocked BEFORE {verb} ON {INSTANCE_TABLE} BEGIN SELECT RAISE(ABORT,'OMS instance identity is immutable'); END")
        db.commit()
        return value
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def empty_oms_fingerprint(db):
    require_empty_oms(db)
    # Exclude only this module's identity schema, so re-provisioning is idempotent.
    schema = [dict(r) for r in db.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name")
        if r['tbl_name'] != INSTANCE_TABLE]
    meta = [dict(r) for r in db.execute('SELECT * FROM oms_meta ORDER BY id')]
    return digest({'schema': schema, 'meta': meta})


def inspect_oms(path):
    with read_database(path) as db:
        result = {'empty_schema_sha256': empty_oms_fingerprint(db),
                  'instance_uuid': None}
        if INSTANCE_TABLE in _tables(db):
            result['instance_uuid'] = oms_instance(db)
        return result


def inspect_transition(ledger_path, oms_path, *, tenant, catalog, account_binding,
                       catalog_capture_sha256, paper_only):
    """Produce private review material, not permission to apply or start a daemon.

    Binding and capture hashes are caller declarations, not source authentication.
    Separate read transactions require writer quiescence for cross-database consistency.
    """
    from quant_ai.domain.models import AssetClass, Market
    from quant_ai.execution.reconciliation import reconcile_paper
    from quant_ai.governance.runtime_identity import path_digest
    from quant_ai.instruments.identity import canonical_instrument_identity

    if paper_only is not True:
        raise ValueError('transition_paper_only_required')
    if not isinstance(tenant, str) or not tenant.strip() or tenant != tenant.strip():
        raise ValueError('transition_tenant_invalid')
    for value in (account_binding, catalog_capture_sha256):
        if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
            raise ValueError('transition_review_binding_required')
    instruments = tuple(catalog)
    if not instruments or len(instruments) > 50 or len({i.symbol for i in instruments}) != len(instruments):
        raise ValueError('transition_catalog_scope')
    if any(i.market is not Market.INDIA or i.exchange != 'NSE' or i.currency != 'INR'
           or i.asset_class not in (AssetClass.EQUITY, AssetClass.ETF)
           or i.is_dated_contract or i.tradable is not True for i in instruments):
        raise ValueError('transition_catalog_nse_cash_only')
    identities = {i.symbol: canonical_instrument_identity(i) for i in instruments}
    ledger, oms = _private_existing(ledger_path), _private_existing(oms_path)
    if ledger.samefile(oms):
        raise ValueError('transition_distinct_stores_required')
    with read_database(ledger) as db, read_database(oms) as odb:
        require_empty_oms(odb)
        instance = oms_instance(odb)
        tables = _tables(db)
        if 'paper_identity_transitions' in tables or 'paper_runtime_order_identity' in tables:
            for table in ('paper_identity_transitions', 'paper_runtime_order_identity'):
                if table in tables and db.execute(f'SELECT 1 FROM {table} WHERE tenant_id=?', (tenant,)).fetchone():
                    raise ValueError('transition_existing_pin_requires_review')
        required = ('paper_accounts', 'paper_positions', 'paper_ledger', 'paper_cost_ledger',
                    'paper_decision_evidence', 'paper_protection_evidence', 'paper_protective_fill_outbox',
                    'paper_idempotency')
        if not set(required) <= tables:
            raise ValueError('transition_ledger_schema_missing')
        inventory = {table: sorted((dict(r) for r in db.execute(
            f'SELECT * FROM {table} WHERE tenant_id=?', (tenant,))), key=canonical)
            for table in required}
        if len(inventory['paper_accounts']) != 1 or not inventory['paper_ledger']:
            raise ValueError('transition_legacy_account_required')
        for row in inventory['paper_positions']:
            if type(row['quantity']) is not int or row['quantity'] <= 0:
                raise ValueError('transition_physical_zero_or_invalid_position')
            if (row['symbol'] not in identities or row['market'] != 'INDIA'
                    or row['asset_class'] not in ('EQUITY', 'ETF')
                    or row['instrument_identity'] is not None):
                raise ValueError('transition_held_identity_ambiguous')
        for row in inventory['paper_ledger']:
            if (row['market'] != 'INDIA' or row['asset_class'] not in ('EQUITY', 'ETF')
                    or row['instrument_identity'] is not None or row['status'] != 'FILLED'):
                raise ValueError('transition_legacy_cohort_ambiguous')
        validate_legacy_evidence(inventory)
        checked = reconcile_paper(db, tenant)
        if checked['status'] != 'matched':
            raise ValueError('transition_legacy_reconciliation_failed')
        plan = {'schema': 'pramana.paper_identity_transition_plan.v1', 'tenant_id': tenant,
                'account_binding': account_binding, 'catalog_capture_sha256': catalog_capture_sha256,
                'instruments': identities, 'legacy_inventory': inventory,
                'ledger_path_sha256': path_digest(ledger), 'oms_path_sha256': path_digest(oms),
                'oms_instance_uuid': instance, 'oms_empty_sha256': empty_oms_fingerprint(odb),
                'forward_only': True, 'apply_supported': False}
        return {'plan': plan, 'sha256': digest(plan)}


def validate_legacy_evidence(inventory):
    """Validate present fill links; missing historical evidence remains unknown."""
    fills = {r['order_id']: r for r in inventory['paper_ledger']}
    if len(fills) != len(inventory['paper_ledger']):
        raise ValueError('transition_duplicate_legacy_order')
    protected = {}
    decision = set()
    for table in ('paper_protection_evidence', 'paper_decision_evidence'):
        seen = set()
        for row in inventory[table]:
            order_id = row['order_id']
            if order_id not in fills or order_id in seen:
                raise ValueError('transition_orphan_or_duplicate_evidence')
            seen.add(order_id)
            fill = fills[order_id]
            try:
                from datetime import datetime
                from decimal import Decimal
                payload = json.loads(row['payload'])
                valid = (payload['order_id'] == order_id
                         and payload['tenant_id'] == fill['tenant_id']
                         and payload['subject'] == fill['symbol']
                         and payload['fill']['quantity'] == fill['quantity']
                         and payload['fill']['status'] == 'FILLED'
                         and Decimal(str(payload['fill']['price'])) == Decimal(fill['fill_price'])
                         and datetime.fromisoformat(payload['filled_at']) == datetime.fromisoformat(fill['created_at']))
            except (KeyError, TypeError, ValueError, ArithmeticError):
                raise ValueError('transition_legacy_evidence_invalid') from None
            if not valid:
                raise ValueError('transition_legacy_evidence_invalid')
            if table == 'paper_protection_evidence':
                if fill['side'] != 'SELL' or payload.get('schema') != 'pramana.protective_exit.v1':
                    raise ValueError('transition_protective_evidence_invalid')
                protected[order_id] = hashlib.sha256(row['payload'].encode()).hexdigest()
            else:
                decision.add(order_id)
    outbox = {r['order_id']: r['receipt_sha256'] for r in inventory['paper_protective_fill_outbox']}
    if (len(outbox) != len(inventory['paper_protective_fill_outbox'])
            or outbox != protected or decision & protected.keys()):
        raise ValueError('transition_protective_outbox_invalid')


def apply_reviewed_transition(ledger_path, oms_path, *, reviewed_plan_sha256,
                              tenant, catalog, account_binding, catalog_capture_sha256,
                              paper_only):
    """Forward-only local apply primitive, not wired to any CLI or runtime activation.

    Hold write locks on BOTH databases during revalidation/ledger commit. OMS already
    has an explicit provisioned identity; this operation writes only the ledger.
    """
    from quant_ai.governance.runtime_identity import SCHEMA as PIN_SCHEMA

    if paper_only is not True:
        raise ValueError('transition_paper_only_required')
    ledger, oms = _private_existing(ledger_path), _private_existing(oms_path)
    if ledger.samefile(oms):
        raise ValueError('transition_distinct_stores_required')
    db, odb = sqlite3.connect(ledger, timeout=2), sqlite3.connect(oms, timeout=2)
    try:
        odb.execute('BEGIN IMMEDIATE')
        db.execute('BEGIN IMMEDIATE')
        db.row_factory = sqlite3.Row
        odb.row_factory = sqlite3.Row
        if 'paper_identity_transitions' in _tables(db):
            prior = db.execute('SELECT sha256 FROM paper_identity_transitions WHERE tenant_id=?', (tenant,)).fetchone()
            if prior is not None:
                from quant_ai.governance.runtime_identity import path_digest
                from quant_ai.instruments.identity import canonical_instrument_identity
                transition = TransitionReplay(db, tenant)
                plan = transition.plan
                if (prior['sha256'] != reviewed_plan_sha256
                        or plan['account_binding'] != account_binding
                        or plan['catalog_capture_sha256'] != catalog_capture_sha256
                        or plan['instruments'] != {i.symbol: canonical_instrument_identity(i) for i in catalog}
                        or plan['oms_instance_uuid'] != oms_instance(odb)
                        or plan['oms_path_sha256'] != path_digest(oms)
                        or plan['ledger_path_sha256'] != path_digest(ledger)):
                    raise ValueError('transition_reapply_conflict')
                db.rollback()
                odb.rollback()
                return reviewed_plan_sha256
        inspected = inspect_transition(ledger, oms, tenant=tenant, catalog=catalog,
            account_binding=account_binding, catalog_capture_sha256=catalog_capture_sha256,
            paper_only=paper_only)
        if inspected['sha256'] != reviewed_plan_sha256:
            raise ValueError('transition_reviewed_plan_changed')
        plan = inspected['plan']
        db.execute('CREATE TABLE IF NOT EXISTS paper_identity_transitions(tenant_id TEXT PRIMARY KEY,payload TEXT NOT NULL,sha256 TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS paper_runtime_order_identity(tenant_id TEXT PRIMARY KEY,payload TEXT NOT NULL,sha256 TEXT NOT NULL)')
        for table, prefix in (('paper_identity_transitions', 'paper_identity_transition'),
                              ('paper_runtime_order_identity', 'runtime_order_identity')):
            for verb in ('UPDATE', 'DELETE'):
                db.execute(f"CREATE TRIGGER IF NOT EXISTS {prefix}_{verb.lower()}_blocked BEFORE {verb} ON {table} BEGIN SELECT RAISE(ABORT,'Paper identity transition is immutable'); END")
        raw = canonical(plan)
        db.execute('INSERT INTO paper_identity_transitions VALUES(?,?,?)',
                   (tenant, raw, inspected['sha256']))
        pin = {'schema': PIN_SCHEMA, 'mode': 'bound_v1', 'instruments': plan['instruments'],
               'oms_path_sha256': plan['oms_path_sha256']}
        db.execute('INSERT INTO paper_runtime_order_identity VALUES(?,?,?)', (tenant, canonical(pin), digest(pin)))
        for row in plan['legacy_inventory']['paper_positions']:
            changed = db.execute('UPDATE paper_positions SET instrument_identity=? WHERE tenant_id=? AND symbol=? AND market=? AND asset_class=? AND instrument_identity IS NULL',
                (plan['instruments'][row['symbol']], tenant, row['symbol'], row['market'], row['asset_class']))
            if changed.rowcount != 1:
                raise ValueError('transition_position_changed')
        db.commit()
        odb.rollback()  # No OMS writes; release lock only after ledger commit.
        return inspected['sha256']
    except BaseException:
        db.rollback()
        odb.rollback()
        raise
    finally:
        db.close()
        odb.close()


class TransitionReplay:
    """One certified effective-identity boundary shared by replay consumers.

    Source rows are never rewritten. Consumers still perform their normal geometry,
    cash, protection and receipt validations around this identity-only boundary.
    """
    def __init__(self, db, tenant):
        self.plan = None
        self.last = {}
        self.positions = {}
        if 'paper_identity_transitions' not in _tables(db):
            return
        row = db.execute('SELECT payload,sha256 FROM paper_identity_transitions WHERE tenant_id=?', (tenant,)).fetchone()
        if row is None:
            return
        try:
            plan = json.loads(row['payload'])
            if (canonical(plan) != row['payload'] or digest(plan) != row['sha256']
                    or plan['schema'] != 'pramana.paper_identity_transition_plan.v1'
                    or plan['tenant_id'] != tenant or plan['forward_only'] is not True):
                raise ValueError('transition_certificate_invalid')
            original = plan['legacy_inventory']
            account = db.execute('SELECT starting_capital FROM paper_accounts WHERE tenant_id=?', (tenant,)).fetchone()
            if account is None or account['starting_capital'] != original['paper_accounts'][0]['starting_capital']:
                raise ValueError('transition_original_capital_changed')
            if 'paper_idempotency' in original:
                expected_claims = {r['key']: r for r in original['paper_idempotency']}
                current_claims = {r['key']: dict(r) for r in db.execute('SELECT * FROM paper_idempotency WHERE tenant_id=?', (tenant,))}
                if any(current_claims.get(key) != value for key, value in expected_claims.items()):
                    raise ValueError('transition_original_claims_changed')
            ids = {r['order_id'] for r in original['paper_ledger']}
            limit = max(r['id'] for r in original['paper_ledger'])
            for table in ('paper_ledger', 'paper_cost_ledger', 'paper_decision_evidence',
                          'paper_protection_evidence', 'paper_protective_fill_outbox'):
                current = [dict(r) for r in db.execute(f'SELECT * FROM {table} WHERE tenant_id=?', (tenant,))]
                cohort = sorted((r for r in current if r['order_id'] in ids), key=canonical)
                if cohort != original[table]:
                    raise ValueError('transition_legacy_inventory_changed')
                if table == 'paper_ledger' and any(r['id'] <= limit or r['instrument_identity'] is None
                                                  for r in current if r['order_id'] not in ids):
                    raise ValueError('transition_unexpected_low_id_fill')
            for fill in sorted(original['paper_ledger'], key=lambda r: r['id']):
                self.last[fill['symbol']] = fill['order_id']
            self.positions = {r['symbol']: r for r in original['paper_positions']}
            self.plan = plan
        except (KeyError, TypeError, ValueError):
            raise ValueError('transition_certificate_or_cohort_invalid') from None

    def after_fill(self, order_id, symbol, held, average, identity):
        if self.plan is None or self.last.get(symbol) != order_id:
            return identity
        expected = self.positions.get(symbol)
        if expected is None:
            if held != 0:
                raise ValueError('transition_replayed_position_mismatch')
            return None
        from decimal import Decimal
        if (held != expected['quantity'] or average != Decimal(expected['average_price'])
                or identity != expected['instrument_identity']):
            raise ValueError('transition_replayed_position_mismatch')
        return self.plan['instruments'][symbol]
