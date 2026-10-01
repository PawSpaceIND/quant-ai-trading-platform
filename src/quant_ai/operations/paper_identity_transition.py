"""Local paper identity transition primitives; no provider or runtime activation.

Provisioning is an explicit database write. Inspection never creates files or schema.
This module is not yet a complete migration/apply interface.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

OMS_HISTORY = ('oms_orders', 'oms_events', 'oms_fills',
               'oms_broker_evidence_bindings', 'oms_replacements')
INSTANCE_TABLE = 'pramana_oms_instance'
# Generated from the supported fresh DurableOms v1 schema, including constraints
# and append-only triggers. An arbitrary caller-reviewed hash is not qualification.
TRUSTED_EMPTY_OMS_SHA256 = 'db97472fc1e35ac56e5ca827bf55ed0940b92c3eaa958669b40ac1ccbf778da9'
BROKER_CORE_TABLES = frozenset({
    'paper_accounts', 'paper_positions', 'paper_ledger', 'paper_derivative_margin',
    'paper_cost_ledger', 'paper_protection_evidence', 'paper_protective_fill_outbox',
    'paper_decision_evidence', 'paper_idempotency', 'paper_exit_cooldowns', 'sqlite_sequence',
})
# Complete _create_schema + _EXPECTED_COLUMNS logical schema, constraints/index
# definitions and required broker triggers; harmless column ordering is ignored. Missing objects must not be repaired AFTER certification.
# Only audited initializer-produced SQL variants are accepted. The second is
# peak_equity repaired by _EXPECTED_COLUMNS before planning, not after certification.
TRUSTED_BROKER_CORE_DDL_SHA256 = frozenset({
    'f0eba5f5eea66310ab140b12b848db51cf373890fdf3b27f500f147e050c18a1',
    '8583002368d95f26f9128b800d561b5325406dc7806a72d0f07a0aa2483b2939',
})
TRUSTED_PILOT_SCOPE_DDL_SHA256 = '244486444f8129ba53bc001479a40866e6158728d87e68e2852339771835abb9'
TRUSTED_BROKER_CORE_SHA256 = 'e8ba2c0e3ac35a1e2e34ac52784f6f9400e0a88a9ed0814ace43673662589683'
TRUSTED_RUNTIME_SCHEMA_SHA256 = '4ef266017e804319ec946dc5ca9335b4df373057ee3fb2fc0cd042c9eb47fb2d'
SUPPORTED_RUNTIME_TABLES = frozenset({
    'paper_decision_journal', 'paper_live_valuations', 'paper_specialist_feedback',
    'pilot_feed_minutes', 'pilot_runtime', 'pilot_strategy_manifests',
    'risk_control_state', 'risk_daily_equity',
})
TRUSTED_LEDGER_TRIGGERS = {
    'paper_specialist_feedback_delete_blocked': '31036af5664c200731482715c159e070794483c3288ca9038ea8a0feae9635f0',
    'paper_specialist_feedback_update_blocked': '3022f74dbc8bcdf2425298a81422cccb0ca4d52d40b20313effbe4b3052a3fae',
    'protective_outbox_delete_blocked': '05afd1c237b74fb08f29ebbc1eaeb30ac33e52754ed4e46d44ba9f9afdc905e3',
    'protective_outbox_update_blocked': 'b20124ae55f5b995939fc055ee0cc45b5d37857adcf86de42930061dda01ad01',
    'shared_broker_witness_delete_blocked': 'a5eb7903e15e5a710fb1452fc020d99444bf1514209d1398600c20358808a343',
    'shared_broker_witness_update_blocked': 'b5603a9bc16dc867b33c9571c0792ccffb03a7933046d0d8027436f1e04e5df6',
}



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
    if _oms_schema_fingerprint(db) != TRUSTED_EMPTY_OMS_SHA256:
        raise ValueError('transition_oms_supported_schema_required')


def oms_instance(db):
    if INSTANCE_TABLE not in _tables(db):
        raise ValueError('transition_oms_instance_missing')
    validate_instance_schema(db)
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


def _check_review_window(not_before, not_after, clock):
    if not_before is None and not_after is None:
        return  # Original direct library callers retain their existing contract.
    current = (clock or (lambda: datetime.now(timezone.utc)))()
    if (not isinstance(not_before, datetime) or not isinstance(not_after, datetime)
            or not isinstance(current, datetime) or not_before.utcoffset() is None
            or not_after.utcoffset() is None or current.utcoffset() is None
            or not not_before <= current < not_after):
        raise ValueError('transition_reviewed_deadline_expired')


def provision_empty_oms(path, *, paper_only, reviewed_empty_sha256, reviewed_state_sha256=None,
                        not_before=None, not_after=None, clock=None):
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
        _check_review_window(not_before, not_after, clock)
        if reviewed_state_sha256 is not None and store_snapshot_sha256(db) != reviewed_state_sha256:
            raise ValueError('transition_reviewed_store_changed')
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
        _check_review_window(not_before, not_after, clock)
        db.commit()
        return value
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def empty_oms_fingerprint(db):
    require_empty_oms(db)
    return _oms_schema_fingerprint(db)


def _oms_schema_fingerprint(db):
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
                    'paper_idempotency', 'pilot_scope')
        if not SUPPORTED_RUNTIME_TABLES <= tables:
            raise ValueError('transition_runtime_schema_initialization_required')
        runtime_schema = {r['name']: ' '.join(r['sql'].split()) for r in schema_rows(db)
                          if r['tbl_name'] in SUPPORTED_RUNTIME_TABLES}
        if digest(runtime_schema) != TRUSTED_RUNTIME_SCHEMA_SHA256:
            raise ValueError('transition_runtime_schema_initialization_required')
        if 'pilot_scope' not in tables:
            raise ValueError('transition_pilot_schema_initialization_required')
        if not set(required) <= tables:
            raise ValueError('transition_ledger_schema_missing')
        schema = ledger_schema(db)
        inventory = {table: sorted((dict(r) for r in db.execute(
            f'SELECT * FROM {table} WHERE tenant_id=?', (tenant,))), key=canonical)
            for table in required}
        if len(inventory['paper_accounts']) != 1 or not inventory['paper_ledger']:
            raise ValueError('transition_legacy_account_required')
        for row in inventory['paper_positions']:
            if type(row['quantity']) is not int or row['quantity'] <= 0:
                raise ValueError('transition_physical_zero_or_invalid_position')
            if (row['symbol'] not in identities or row['market'] != 'INDIA'
                    or row['asset_class'] != next(i.asset_class.value for i in instruments if i.symbol == row['symbol'])
                    or row['instrument_identity'] is not None):
                raise ValueError('transition_held_identity_ambiguous')
        validate_pilot_scope(db, tenant, identities)
        for row in inventory['paper_ledger']:
            if (row['market'] != 'INDIA' or row['asset_class'] not in ('EQUITY', 'ETF')
                    or row['instrument_identity'] is not None or row['status'] != 'FILLED'):
                raise ValueError('transition_legacy_cohort_ambiguous')
        validate_legacy_evidence(inventory)
        from quant_ai.execution.protection_state import protection_coverage
        if protection_coverage(db, tenant)['status'] != 'complete':
            raise ValueError('transition_legacy_protection_incomplete')
        checked = reconcile_paper(db, tenant)
        if checked['status'] != 'matched':
            raise ValueError('transition_legacy_reconciliation_failed')
        plan = {'schema': 'pramana.paper_identity_transition_plan.v1', 'tenant_id': tenant,
                'account_binding': account_binding, 'catalog_capture_sha256': catalog_capture_sha256,
                'instruments': identities, 'legacy_inventory': inventory, 'ledger_schema': schema,
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
                              paper_only, reviewed_store_snapshots=None,
                              not_before=None, not_after=None, clock=None):
    """Forward-only primitive; guarded offline CLI is separate from runtime activation.

    Hold write locks on BOTH databases during revalidation/ledger commit. OMS already
    has an explicit provisioned identity; this operation writes only the ledger.
    Optional complete store snapshots bind verified backups under both write locks.
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
        _check_review_window(not_before, not_after, clock)
        db.row_factory = sqlite3.Row
        odb.row_factory = sqlite3.Row
        if reviewed_store_snapshots is not None and (
                set(reviewed_store_snapshots) != {'ledger', 'oms'}
                or reviewed_store_snapshots['ledger'] != store_snapshot_sha256(db)
                or reviewed_store_snapshots['oms'] != store_snapshot_sha256(odb)):
            raise ValueError('transition_reviewed_store_changed')
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
                _check_review_window(not_before, not_after, clock)
                db.rollback()
                odb.rollback()
                return reviewed_plan_sha256
        inspected = inspect_transition(ledger, oms, tenant=tenant, catalog=catalog,
            account_binding=account_binding, catalog_capture_sha256=catalog_capture_sha256,
            paper_only=paper_only)
        if inspected['sha256'] != reviewed_plan_sha256:
            raise ValueError('transition_reviewed_plan_changed')
        plan = inspected['plan']
        before_rows = all_rows(db)
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
        validate_post_transition(db, plan, before_rows)
        _check_review_window(not_before, not_after, clock)
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
            current_schema = {r['name']: r for r in schema_rows(db)}
            expected_added = transition_schema_sql()
            original_names = {r['name'] for r in plan['ledger_schema']}
            added = {name: ' '.join(row['sql'].split()) for name, row in current_schema.items() if name not in original_names}
            if added != expected_added:
                raise ValueError('transition_unreviewed_post_schema')
            if any(current_schema.get(r['name']) != r for r in plan['ledger_schema']):
                raise ValueError('transition_ledger_schema_changed')
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
                self.last[holding_key(fill)] = fill['order_id']
            self.positions = {holding_key(r): r for r in original['paper_positions']}
            self.plan = plan
        except (KeyError, TypeError, ValueError):
            raise ValueError('transition_certificate_or_cohort_invalid') from None

    def after_fill(self, order_id, key, held, average, identity):
        if self.plan is None or self.last.get(key) != order_id:
            return identity
        expected = self.positions.get(key)
        if expected is None:
            if held != 0:
                raise ValueError('transition_replayed_position_mismatch')
            return None
        from decimal import Decimal
        if (held != expected['quantity'] or average != Decimal(expected['average_price'])
                or identity != expected['instrument_identity']):
            raise ValueError('transition_replayed_position_mismatch')
        return self.plan['instruments'][key[0]]


def holding_key(row):
    return row['symbol'], row['market'], row['asset_class']


def schema_rows(db):
    return [dict(r) for r in db.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name")]


def ledger_schema(db):
    rows = schema_rows(db)
    for row in rows:
        if row['type'] == 'trigger':
            expected = TRUSTED_LEDGER_TRIGGERS.get(row['name'])
            actual = hashlib.sha256(' '.join(row['sql'].split()).encode()).hexdigest()
            if actual != expected:
                raise ValueError('transition_unreviewed_ledger_trigger')
    core_ddl = {r['name']: ' '.join(r['sql'].split()) for r in rows if r['tbl_name'] in BROKER_CORE_TABLES}
    if (digest(core_ddl) not in TRUSTED_BROKER_CORE_DDL_SHA256
            or digest(broker_core_contract(db)) != TRUSTED_BROKER_CORE_SHA256):
        raise ValueError('transition_broker_schema_initialization_required')
    return rows


def all_rows(db):
    return {table: sorted((dict(r) for r in db.execute(f'SELECT * FROM "{table}"')), key=canonical)
            for table in sorted(_tables(db))}


def store_snapshot_sha256(db):
    """Bind a reviewed backup's complete logical schema/rows under operation locks."""
    return digest({'schema': schema_rows(db), 'rows': all_rows(db)})


def validate_post_transition(db, plan, before):
    """Only current holding identity may change; validate inside the write transaction."""
    from quant_ai.execution.protection_state import protection_coverage
    from quant_ai.execution.reconciliation import reconcile_paper
    after = all_rows(db)
    for table, rows in before.items():
        expected = rows
        if table == 'paper_positions':
            expected = [dict(r) for r in rows]
            for row in expected:
                if row['tenant_id'] == plan['tenant_id']:
                    row['instrument_identity'] = plan['instruments'][row['symbol']]
            expected.sort(key=canonical)
        if after.get(table) != expected:
            raise ValueError('transition_post_conservation_failed')
    if reconcile_paper(db, plan['tenant_id'])['status'] != 'matched':
        raise ValueError('transition_post_reconciliation_failed')
    if protection_coverage(db, plan['tenant_id'])['status'] != 'complete':
        raise ValueError('transition_post_protection_failed')


def validate_instance_schema(db):
    expected = {
        INSTANCE_TABLE: f'CREATE TABLE {INSTANCE_TABLE}(singleton INTEGER PRIMARY KEY CHECK(singleton=1),instance_uuid TEXT NOT NULL UNIQUE)',
    }
    for verb in ('UPDATE', 'DELETE'):
        expected[f'oms_instance_{verb.lower()}_blocked'] = (
            f"CREATE TRIGGER oms_instance_{verb.lower()}_blocked BEFORE {verb} ON {INSTANCE_TABLE} "
            "BEGIN SELECT RAISE(ABORT,'OMS instance identity is immutable'); END")
    actual = {row['name']: ' '.join(row['sql'].split()) for row in schema_rows(db)
              if row['tbl_name'] == INSTANCE_TABLE}
    if actual != {name: ' '.join(sql.split()) for name, sql in expected.items()}:
        raise ValueError('transition_oms_instance_schema_invalid')


def transition_schema_sql():
    result = {}
    for table, prefix in (('paper_identity_transitions', 'paper_identity_transition'),
                          ('paper_runtime_order_identity', 'runtime_order_identity')):
        result[table] = f'CREATE TABLE {table}(tenant_id TEXT PRIMARY KEY,payload TEXT NOT NULL,sha256 TEXT NOT NULL)'
        for verb in ('UPDATE', 'DELETE'):
            result[f'{prefix}_{verb.lower()}_blocked'] = (
                f"CREATE TRIGGER {prefix}_{verb.lower()}_blocked BEFORE {verb} ON {table} "
                "BEGIN SELECT RAISE(ABORT,'Paper identity transition is immutable'); END")
    return {name: ' '.join(sql.split()) for name, sql in result.items()}


def validate_pilot_scope(db, tenant, identities):
    sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='pilot_scope'").fetchone()[0]
    if hashlib.sha256(' '.join(sql.split()).encode()).hexdigest() != TRUSTED_PILOT_SCOPE_DDL_SHA256:
        raise ValueError('transition_pilot_schema_initialization_required')
    expected_columns = [('tenant_id', 'TEXT', 0, 1), ('currency', 'TEXT', 1, 0),
                        ('market', 'TEXT', 1, 0), ('symbols', 'TEXT', 1, 0),
                        ('instrument_identities', 'TEXT', 0, 0)]
    columns = [(r['name'], r['type'], r['notnull'], r['pk']) for r in db.execute('PRAGMA table_info(pilot_scope)')]
    if columns != expected_columns:
        raise ValueError('transition_pilot_schema_initialization_required')
    rows = db.execute('SELECT * FROM pilot_scope WHERE tenant_id=?', (tenant,)).fetchall()
    if len(rows) != 1 or rows[0]['currency'] != 'INR' or rows[0]['market'] != 'INDIA':
        raise ValueError('transition_pilot_scope_mismatch')
    try:
        configured = json.loads(rows[0]['instrument_identities'])
        symbols = json.loads(rows[0]['symbols'])
        target = {symbol: json.loads(raw) for symbol, raw in identities.items()}
        classes = {symbol: value['assetClass'] for symbol, value in target.items()}
        if configured != target or symbols != classes:
            raise ValueError('transition_pilot_scope_mismatch')
    except (TypeError, KeyError, ValueError):
        raise ValueError('transition_pilot_scope_mismatch') from None


def broker_core_contract(db):
    """Logical schema contract independent of harmless ALTER column ordering."""
    if not BROKER_CORE_TABLES <= _tables(db):
        return None
    tables = {}
    for table in sorted(BROKER_CORE_TABLES):
        columns = {r['name']: {key: r[key] for key in ('type', 'notnull', 'dflt_value', 'pk')}
                   for r in db.execute(f'PRAGMA table_info("{table}")')}
        indexes = []
        for index in db.execute(f'PRAGMA index_list("{table}")'):
            name = index['name'].replace('"', '""')
            indexes.append({'unique': index['unique'], 'origin': index['origin'], 'partial': index['partial'],
                'columns': [{k: r[k] for k in ('name', 'desc', 'coll', 'key')}
                            for r in db.execute(f'PRAGMA index_xinfo("{name}")')]})
        sql = db.execute('SELECT sql FROM sqlite_master WHERE type=? AND name=?', ('table', table)).fetchone()[0].upper()
        tables[table] = {'columns': columns, 'indexes': sorted(indexes, key=canonical),
            'foreign_keys': [dict(r) for r in db.execute(f'PRAGMA foreign_key_list("{table}")')],
            'autoincrement': 'AUTOINCREMENT' in sql, 'check_constraints': sql.count('CHECK'),
            'without_rowid': 'WITHOUT ROWID' in sql, 'strict': sql.rstrip().endswith('STRICT')}
    triggers = {r['name']: ' '.join(r['sql'].split()) for r in schema_rows(db)
                if r['type'] == 'trigger' and r['tbl_name'] in BROKER_CORE_TABLES}
    return {'tables': tables, 'triggers': triggers}
