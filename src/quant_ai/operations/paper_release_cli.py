"""Default read-only PAPER qualification; guarded, forward-only offline writes.

No daemon, provider, process-control, configuration editing or inverse migration.
Attestations bind reviewed declarations; they do not authenticate process shutdown.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from quant_ai.domain.models import AssetClass, Instrument, Market
from quant_ai.operations.paper_identity_transition import (
    TransitionReplay,
    _private_existing,
    apply_reviewed_transition,
    digest,
    inspect_oms,
    inspect_transition,
    ledger_schema,
    oms_instance,
    provision_empty_oms,
    read_database,
    schema_rows,
    store_snapshot_sha256,
)

ROLES = frozenset({'ledger', 'oms', 'ai_budget', 'ai_spend', 'console'})
WRITERS = frozenset({'engine', 'protection', 'collector', 'dashboard', 'tokenwatch', 'operator_jobs'})


def _check(condition):
    if not condition:
        raise ValueError('release_preflight_refused')


def _hex(value, length=64):
    return type(value) is str and len(value) == length and all(c in '0123456789abcdef' for c in value)


def _hash_file(path):
    with Path(path).open('rb') as source:
        result = hashlib.sha256()
        for chunk in iter(lambda: source.read(65536), b''):
            result.update(chunk)
        return result.hexdigest()


def _json_file(path, *, private=True):
    path = Path(path)
    if private:
        _private_existing(path)
    else:
        _check(path.is_file() and not path.is_symlink() and path.stat().st_nlink == 1)
    _check(path.stat().st_size <= 1048576)
    raw = path.read_bytes()
    # Duplicate declarations are ambiguous, not last-one-wins.
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _check(key not in result)
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique), hashlib.sha256(raw).hexdigest()


def _catalog(path):
    value, sha = _json_file(path)
    _check(type(value) is dict and set(value) == {'schema', 'instruments'}
           and value['schema'] == 'pramana.paper_release_catalog.v1'
           and type(value['instruments']) is list and 0 < len(value['instruments']) <= 50)
    fields = {'symbol', 'market', 'asset_class', 'currency', 'exchange', 'tradable'}
    rows = []
    for row in value['instruments']:
        _check(type(row) is dict and set(row) == fields and row['tradable'] is True
               and type(row['symbol']) is str and row['symbol'].strip() == row['symbol'] and row['symbol']
               and row['market'] == 'INDIA' and row['exchange'] == 'NSE' and row['currency'] == 'INR'
               and row['asset_class'] in ('EQUITY', 'ETF'))
        rows.append(Instrument(row['symbol'], Market(row['market']), AssetClass(row['asset_class']),
                               row['currency'], row['exchange'], tradable=True))
    _check(len({item.symbol for item in rows}) == len(rows))
    return tuple(rows), sha


def _instant(value):
    result = datetime.fromisoformat(value)
    _check(result.utcoffset() is not None)
    return result.astimezone(timezone.utc)


def _arguments(options):
    catalog, capture = _catalog(options.catalog)
    _check(_hex(options.account_binding) and type(options.tenant) is str
           and options.tenant and options.tenant.strip() == options.tenant)
    return {'tenant': options.tenant, 'catalog': catalog, 'account_binding': options.account_binding,
            'catalog_capture_sha256': capture, 'paper_only': True}


def qualify(options):
    arguments = _arguments(options)
    with read_database(options.ledger) as db:
        _check(db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok')
        existing = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_identity_transitions'").fetchone()
        if existing and db.execute('SELECT 1 FROM paper_identity_transitions WHERE tenant_id=?',
                                   (options.tenant,)).fetchone():
            from quant_ai.execution.protection_state import protection_coverage
            from quant_ai.execution.reconciliation import reconcile_paper
            from quant_ai.governance.runtime_identity import path_digest
            from quant_ai.instruments.identity import canonical_instrument_identity

            _check(options.action == 'qualify')
            transition = TransitionReplay(db, options.tenant)
            plan = transition.plan
            with read_database(options.oms) as odb:
                _check(plan['oms_instance_uuid'] == oms_instance(odb)
                       and plan['oms_path_sha256'] == path_digest(options.oms)
                       and plan['ledger_path_sha256'] == path_digest(options.ledger)
                       and plan['account_binding'] == arguments['account_binding']
                       and plan['catalog_capture_sha256'] == arguments['catalog_capture_sha256']
                       and plan['instruments'] == {i.symbol: canonical_instrument_identity(i) for i in arguments['catalog']})
            return {'schema': 'pramana.paper_release_qualification.v1',
                    'status': 'transitioned_forward_recovery_only', 'transition_sha256': digest(plan),
                    'ledger_schema_sha256': digest(schema_rows(db)),
                    'reconciliation': reconcile_paper(db, options.tenant)['status'],
                    'protection': protection_coverage(db, options.tenant)['status'],
                    'oms_order_linkage_verified': False, 'source_authenticity_verified': False,
                    'activation_authorized': False, 'trading_authorized': False}
        schema = digest(ledger_schema(db))
    oms = inspect_oms(options.oms)
    result = {'schema': 'pramana.paper_release_qualification.v1',
              'oms_empty_sha256': oms['empty_schema_sha256'], 'ledger_schema_sha256': schema,
              'catalog_capture_sha256': arguments['catalog_capture_sha256'],
              'catalog_count': len(arguments['catalog']), 'source_authenticity_verified': False,
              'activation_authorized': False, 'trading_authorized': False}
    if oms['instance_uuid'] is None:
        _check(options.action != 'plan')
        result['status'] = 'empty_oms_requires_explicit_provision'
    else:
        planned = inspect_transition(options.ledger, options.oms, **arguments)
        result.update(status='read_only_plan', reviewed_plan_sha256=planned['sha256'],
                      legacy_row_counts={name: len(rows) for name, rows in planned['plan']['legacy_inventory'].items()},
                      forward_only=True)
    return result


@contextmanager
def _lock_store(path):
    path = _private_existing(path).resolve()
    db = sqlite3.connect(path, timeout=2)
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA trusted_schema=OFF')
        db.execute('BEGIN IMMEDIATE')
        yield db
    finally:
        db.rollback()
        db.close()


@contextmanager
def _lease(path):
    import fcntl

    path = _private_existing(path)
    # Read-only descriptor; existing lease contents are never changed.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def _preflight(options, arguments, now):
    _check(options.ack_forward_only and options.ack_quiesced_writers and options.ack_protection_interruption)
    for key, expected in (('TRADING_LIVE_MONEY_ACTIVE', 'false'), ('PRAMANA_IBKR_ENABLED', 'false'),
                          ('PRAMANA_PAPER_OPPORTUNITY_SELECTION', 'false'), ('PRAMANA_PILOT_MODE', 'true')):
        _check(os.environ.get(key) == expected)
    value, sha = _json_file(options.preflight)
    fields = {'schema', 'mode', 'live_money_active', 'ibkr_enabled', 'selector_enabled',
              'runtime_state', 'writers', 'protection_interruption_reviewed', 'captured_at', 'expires_at',
              'release_revision', 'account_binding', 'catalog_capture_sha256', 'ledger_schema_sha256',
              'lease_file', 'lease_sha256', 'stores', 'backup_manifests'}
    _check(_hex(options.reviewed_preflight_sha256) and sha == options.reviewed_preflight_sha256
           and type(value) is dict and set(value) == fields)
    _check(value['schema'] == 'pramana.paper_release_preflight.v1' and value['mode'] == 'PAPER'
           and value['live_money_active'] is False and value['ibkr_enabled'] is False
           and value['selector_enabled'] is False and value['runtime_state'] == 'STOPPED'
           and value['protection_interruption_reviewed'] is True)
    _check(type(value['writers']) is dict and set(value['writers']) == WRITERS
           and all(state == 'STOPPED' for state in value['writers'].values()))
    captured, expires = _instant(value['captured_at']), _instant(value['expires_at'])
    _check(captured <= now < expires and timedelta(0) < expires - captured <= timedelta(minutes=15))
    _check(_hex(value['release_revision'], 40) and value['account_binding'] == arguments['account_binding']
           and value['catalog_capture_sha256'] == arguments['catalog_capture_sha256']
           and _hex(value['ledger_schema_sha256']))
    _check(type(value['stores']) is dict and type(value['backup_manifests']) is dict
           and set(value['stores']) == ROLES and set(value['backup_manifests']) == ROLES)
    stores = {role: _private_existing(path).resolve() for role, path in value['stores'].items()}
    _check(len(set(stores.values())) == len(ROLES)
           and stores['ledger'] == Path(options.ledger).resolve() and stores['oms'] == Path(options.oms).resolve())
    lease = _private_existing(value['lease_file']).resolve()
    _check(lease not in stores.values())
    _check(_hex(value['lease_sha256']) and _hash_file(value['lease_file']) == value['lease_sha256'])
    return value, stores


def _backups(value, stores):
    snapshots, destinations = {}, set()
    for role in sorted(ROLES):
        manifest, _ = _json_file(value['backup_manifests'][role], private=False)
        _check(type(manifest) is dict and set(manifest) == {'source', 'backup', 'sha256', 'created_at', 'integrity'}
               and manifest['integrity'] == 'ok' and _hex(manifest['sha256'])
               and Path(manifest['source']).resolve() == stores[role])
        backup = _private_existing(manifest['backup']).resolve()
        _check(backup not in stores.values() and backup not in destinations
               and _instant(manifest['created_at']) <= _instant(value['captured_at'])
               and _hash_file(backup) == manifest['sha256'])
        destinations.add(backup)
        with read_database(backup) as db, read_database(stores[role]) as live:
            _check(db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
                   and live.execute('PRAGMA integrity_check').fetchone()[0] == 'ok')
            snapshots[role] = store_snapshot_sha256(db)
            _check(snapshots[role] == store_snapshot_sha256(live))
            if role == 'ledger':
                _check(digest(ledger_schema(live)) == value['ledger_schema_sha256'])
    return snapshots


def write_offline(options, *, clock=None):
    clock = clock or (lambda: datetime.now(timezone.utc))
    arguments = _arguments(options)
    value, stores = _preflight(options, arguments, clock())
    with _lease(value['lease_file']), ExitStack() as locks:
        # The existing library locks the stores it writes. Lock every OTHER reviewed
        # store throughout backup revalidation and mutation to exclude competing writes.
        owned = {'oms'} if options.action == 'provision-oms' else {'ledger', 'oms'}
        for role in sorted(ROLES - owned):
            locks.enter_context(_lock_store(stores[role]))
        snapshots = _backups(value, stores)
        _check(clock() < _instant(value['expires_at']))
        window = {'not_before': _instant(value['captured_at']), 'not_after': _instant(value['expires_at']),
                  'clock': clock}
        if options.action == 'provision-oms':
            _check(_hex(options.reviewed_empty_oms_sha256))
            provision_empty_oms(stores['oms'], paper_only=True,
                reviewed_empty_sha256=options.reviewed_empty_oms_sha256,
                reviewed_state_sha256=snapshots['oms'], **window)
        else:
            _check(options.action == 'apply' and _hex(options.reviewed_plan_sha256))
            apply_reviewed_transition(stores['ledger'], stores['oms'], **arguments,
                reviewed_plan_sha256=options.reviewed_plan_sha256,
                reviewed_store_snapshots={role: snapshots[role] for role in ('ledger', 'oms')}, **window)
    return {'schema': 'pramana.paper_release_operation.v1', 'status': 'completed', 'operation': options.action,
            'forward_only': True, 'quiescence_attested': True, 'process_shutdown_verified': False,
            'activation_authorized': False, 'trading_authorized': False}


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # Invalid argument values might contain secrets accidentally pasted by an operator.
        raise ValueError('release_arguments_invalid')


def main(argv=None):
    parser = _Parser(description=__doc__)
    parser.add_argument('action', nargs='?', default='qualify', choices=('qualify', 'plan', 'provision-oms', 'apply'))
    for name in ('ledger', 'oms', 'catalog', 'tenant', 'account-binding'):
        parser.add_argument('--' + name, required=True)
    for name in ('preflight', 'reviewed-preflight-sha256', 'reviewed-plan-sha256', 'reviewed-empty-oms-sha256'):
        parser.add_argument('--' + name)
    for name in ('ack-forward-only', 'ack-quiesced-writers', 'ack-protection-interruption'):
        parser.add_argument('--' + name, action='store_true')
    try:
        options = parser.parse_args(argv)
        result = qualify(options) if options.action in ('qualify', 'plan') else write_offline(options)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception:  # noqa: BLE001 - no private paths/rows/provider messages in reports
        print(json.dumps({'schema': 'pramana.paper_release_operation.v1', 'status': 'refused',
                          'reason': 'release_preflight_or_operation_failed', 'activation_authorized': False}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
