"""Synthetic offline release/recovery boundaries; no host, provider or orders."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from test_paper_identity_provisioning import legacy_plan_fixture

from quant_ai.operations import paper_release_cli as cli
from quant_ai.operations.paper_identity_transition import (
    digest,
    inspect_transition,
    read_database,
    store_snapshot_sha256,
)

ROOT = Path(__file__).resolve().parents[1]


def ops():
    spec = importlib.util.spec_from_file_location('release_backup_fixture', ROOT / 'scripts/pilot_ops.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def private_json(path, value):
    path.write_text(json.dumps(value, sort_keys=True))
    path.chmod(0o600)
    return cli._hash_file(path)


def fixture(tmp_path, monkeypatch):
    ledger, oms, arguments, _ = legacy_plan_fixture(tmp_path)
    catalog = tmp_path / 'catalog.json'
    capture = private_json(catalog, {'schema': 'pramana.paper_release_catalog.v1', 'instruments': [
        {'symbol': 'INFY', 'market': 'INDIA', 'asset_class': 'EQUITY', 'currency': 'INR',
         'exchange': 'NSE', 'tradable': True}]})
    arguments['catalog_capture_sha256'] = capture
    planned = inspect_transition(ledger, oms, **arguments)
    stores = {'ledger': ledger, 'oms': oms}
    for role in ('ai_budget', 'ai_spend', 'console'):
        path = tmp_path / (role + '.sqlite')
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE synthetic(id INTEGER PRIMARY KEY,value TEXT)')
            db.execute("INSERT INTO synthetic VALUES(1,'private fixture text')")
        path.chmod(0o600)
        stores[role] = path
    manifests = {}
    for role, source in stores.items():
        destination = tmp_path / (role + '.backup.sqlite')
        ops().backup(source, destination)
        manifests[role] = str(destination.with_suffix(destination.suffix + '.manifest.json'))
    lease = tmp_path / 'offline.lease'
    lease.write_text('synthetic reviewed offline lease')
    lease.chmod(0o600)
    now = datetime.now(timezone.utc)
    preflight = {
        'schema': 'pramana.paper_release_preflight.v1', 'mode': 'PAPER',
        'live_money_active': False, 'ibkr_enabled': False, 'selector_enabled': False,
        'runtime_state': 'STOPPED', 'writers': dict.fromkeys(cli.WRITERS, 'STOPPED'),
        'protection_interruption_reviewed': True, 'captured_at': now.isoformat(),
        'expires_at': (now + timedelta(minutes=5)).isoformat(), 'release_revision': 'a'*40,
        'account_binding': arguments['account_binding'], 'catalog_capture_sha256': capture,
        'ledger_schema_sha256': digest(planned['plan']['ledger_schema']),
        'lease_file': str(lease), 'lease_sha256': cli._hash_file(lease),
        'stores': {role: str(path) for role, path in stores.items()}, 'backup_manifests': manifests,
    }
    path = tmp_path / 'preflight.json'
    sha = private_json(path, preflight)
    options = SimpleNamespace(action='apply', ledger=str(ledger), oms=str(oms), catalog=str(catalog),
        tenant='pilot', account_binding=arguments['account_binding'], preflight=str(path),
        reviewed_preflight_sha256=sha, reviewed_plan_sha256=planned['sha256'],
        reviewed_empty_oms_sha256=planned['plan']['oms_empty_sha256'], ack_forward_only=True,
        ack_quiesced_writers=True, ack_protection_interruption=True)
    for key, value in {'TRADING_LIVE_MONEY_ACTIVE': 'false', 'PRAMANA_IBKR_ENABLED': 'false',
                       'PRAMANA_PAPER_OPPORTUNITY_SELECTION': 'false', 'PRAMANA_PILOT_MODE': 'true'}.items():
        monkeypatch.setenv(key, value)
    return options, preflight, stores, now


def snapshots(stores):
    result = {}
    for role, path in stores.items():
        with read_database(path) as db:
            result[role] = store_snapshot_sha256(db)
    return result


def edit(options, preflight, **changes):
    preflight.update(changes)
    options.reviewed_preflight_sha256 = private_json(Path(options.preflight), preflight)


def argv(options, action=None):
    result = [action or options.action]
    for field in ('ledger', 'oms', 'catalog', 'tenant', 'account_binding', 'preflight',
                  'reviewed_preflight_sha256', 'reviewed_plan_sha256', 'reviewed_empty_oms_sha256'):
        result.extend(('--' + field.replace('_', '-'), getattr(options, field)))
    return result + ['--ack-forward-only', '--ack-quiesced-writers', '--ack-protection-interruption']


def test_default_qualification_and_plan_are_read_only_and_redacted(tmp_path, monkeypatch, capsys):
    options, _, stores, _ = fixture(tmp_path, monkeypatch)
    before = snapshots(stores)
    args = argv(options, 'plan')
    assert cli.main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['reviewed_plan_sha256'] == options.reviewed_plan_sha256
    assert report['status'] == 'read_only_plan'
    assert 'legacy_inventory' not in report
    assert str(tmp_path) not in json.dumps(report)
    assert 'private fixture text' not in json.dumps(report)
    assert snapshots(stores) == before
    assert cli.main(args[1:]) == 0  # Omitted action defaults to read-only qualification.
    assert json.loads(capsys.readouterr().out)['status'] == 'read_only_plan'
    assert snapshots(stores) == before


def test_guarded_apply_conserves_other_stores_and_reconciles_restart(tmp_path, monkeypatch):
    from quant_ai.execution.paper_ledger import PaperBrokerService
    options, _, stores, now = fixture(tmp_path, monkeypatch)
    before = snapshots(stores)
    result = cli.write_offline(options, clock=lambda: now)
    assert result['status'] == 'completed'
    assert result['process_shutdown_verified'] is False
    assert result['activation_authorized'] is result['trading_authorized'] is False
    after = snapshots(stores)
    assert {r: after[r] for r in cli.ROLES - {'ledger'}} == {r: before[r] for r in cli.ROLES - {'ledger'}}
    with read_database(stores['ledger']) as db:
        assert db.execute('SELECT COUNT(*) FROM paper_identity_transitions').fetchone()[0] == 1
    broker = PaperBrokerService(stores['ledger'])
    assert broker.reconcile('pilot')['status'] == 'matched'
    broker.close()
    options.action = 'qualify'
    report = cli.qualify(options)
    assert report['status'] == 'transitioned_forward_recovery_only'
    assert report['reconciliation'] == 'matched'
    assert report['protection'] == 'complete'
    assert report['oms_order_linkage_verified'] is False
    options.action = 'plan'
    with pytest.raises(ValueError):
        cli.qualify(options)  # No new plan can reset a committed identity transition.


@pytest.mark.parametrize('field,value', [
    ('mode', 'LIVE'), ('mode', 'UNKNOWN'), ('runtime_state', 'RUNNING'),
    ('live_money_active', True), ('ibkr_enabled', True), ('selector_enabled', True),
    ('protection_interruption_reviewed', False), ('protection_interruption_reviewed', 1),
    ('account_binding', 'c'*64), ('catalog_capture_sha256', 'c'*64),
    ('ledger_schema_sha256', 'c'*64), ('release_revision', 'unknown')])
def test_unqualified_manifest_refuses_without_data_changes(tmp_path, monkeypatch, field, value):
    options, preflight, stores, now = fixture(tmp_path, monkeypatch)
    edit(options, preflight, **{field: value})
    before = snapshots(stores)
    with pytest.raises(ValueError):
        cli.write_offline(options, clock=lambda: now)
    assert snapshots(stores) == before


@pytest.mark.parametrize('key', ['TRADING_LIVE_MONEY_ACTIVE', 'PRAMANA_IBKR_ENABLED',
                               'PRAMANA_PAPER_OPPORTUNITY_SELECTION', 'PRAMANA_PILOT_MODE'])
@pytest.mark.parametrize('value', [None, 'unknown'])
def test_missing_unknown_effective_guard_refuses(tmp_path, monkeypatch, key, value):
    options, _, stores, now = fixture(tmp_path, monkeypatch)
    if value is None:
        monkeypatch.delenv(key)
    else:
        monkeypatch.setenv(key, value)
    before = snapshots(stores)
    with pytest.raises(ValueError):
        cli.write_offline(options, clock=lambda: now)
    assert snapshots(stores) == before


@pytest.mark.parametrize('ack', ['ack_forward_only', 'ack_quiesced_writers', 'ack_protection_interruption'])
def test_explicit_acknowledgments_required(tmp_path, monkeypatch, ack):
    options, _, stores, now = fixture(tmp_path, monkeypatch)
    setattr(options, ack, False)
    before = snapshots(stores)
    with pytest.raises(ValueError):
        cli.write_offline(options, clock=lambda: now)
    assert snapshots(stores) == before


@pytest.mark.parametrize('change', ['missing_writer', 'running_writer', 'expired', 'future',
                                  'long_lease', 'unreviewed', 'plan', 'lease', 'missing_store', 'same_store'])
def test_quiescence_review_and_scope_refusals(tmp_path, monkeypatch, change):
    options, preflight, stores, now = fixture(tmp_path, monkeypatch)
    if change == 'missing_writer': del preflight['writers']['protection']
    if change == 'running_writer': preflight['writers']['protection'] = 'RUNNING'
    if change == 'expired': preflight['expires_at'] = now.isoformat()
    if change == 'future': preflight['captured_at'] = (now + timedelta(seconds=1)).isoformat()
    if change == 'long_lease': preflight['expires_at'] = (now + timedelta(hours=1)).isoformat()
    if change == 'lease': preflight['lease_sha256'] = 'f'*64
    if change == 'missing_store': del preflight['stores']['console']
    if change == 'same_store': preflight['stores']['console'] = preflight['stores']['ledger']
    edit(options, preflight)
    if change == 'unreviewed': options.reviewed_preflight_sha256 = 'f'*64
    if change == 'plan': options.reviewed_plan_sha256 = 'f'*64
    before = snapshots(stores)
    with pytest.raises(ValueError):
        cli.write_offline(options, clock=lambda: now)
    assert snapshots(stores) == before


@pytest.mark.parametrize('change', ['backup_bytes', 'manifest', 'missing_backup', 'changed_store'])
def test_complete_backup_set_required_and_stale_backups_refused(tmp_path, monkeypatch, change):
    options, preflight, stores, now = fixture(tmp_path, monkeypatch)
    manifest_path = Path(preflight['backup_manifests']['ai_spend'])
    manifest = json.loads(manifest_path.read_text())
    if change == 'backup_bytes': Path(manifest['backup']).write_bytes(b'corrupt fixture')
    if change == 'manifest': manifest['sha256'] = 'f'*64; private_json(manifest_path, manifest)
    if change == 'missing_backup': Path(manifest['backup']).unlink()
    if change == 'changed_store':
        with sqlite3.connect(stores['ai_spend']) as db:
            db.execute("UPDATE synthetic SET value='changed after backup'")
    before = snapshots(stores)
    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        cli.write_offline(options, clock=lambda: now)
    assert snapshots(stores) == before


def test_race_after_backup_check_refuses_inside_existing_apply_locks(tmp_path, monkeypatch):
    options, _, stores, now = fixture(tmp_path, monkeypatch)
    original = cli.apply_reviewed_transition
    def raced(*args, **kwargs):
        with sqlite3.connect(stores['ledger']) as db:
            db.execute("UPDATE paper_accounts SET cash_balance='999' WHERE tenant_id='pilot'")
        return original(*args, **kwargs)
    monkeypatch.setattr(cli, 'apply_reviewed_transition', raced)
    with pytest.raises(ValueError, match='reviewed_store_changed'):
        cli.write_offline(options, clock=lambda: now)
    with read_database(stores['ledger']) as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='paper_identity_transitions'").fetchone()
        assert db.execute('SELECT instrument_identity FROM paper_positions').fetchone()[0] is None


def test_guarded_idempotent_provision(tmp_path, monkeypatch):
    options, _preflight, stores, now = fixture(tmp_path, monkeypatch)
    options.action = 'provision-oms'
    before = snapshots(stores)
    assert cli.write_offline(options, clock=lambda: now)['status'] == 'completed'
    assert snapshots(stores) == before  # Already provisioned fixture is idempotent.
    options.reviewed_empty_oms_sha256 = 'f'*64
    with pytest.raises(ValueError):
        cli.write_offline(options, clock=lambda: now)
    assert snapshots(stores) == before


def test_fresh_provision_requires_new_review_and_backup_before_apply(tmp_path, monkeypatch):
    from quant_ai.orders.oms import DurableOms
    options, preflight, stores, _now = fixture(tmp_path, monkeypatch)
    # Replace only a synthetic fixture, simulating a separately provisioned fresh OMS.
    stores['oms'].rename(tmp_path / 'original-provisioned-oms.sqlite')
    DurableOms(stores['oms']).close()
    manifest = ops().backup(stores['oms'], tmp_path / 'oms.before-provision.sqlite')
    preflight['backup_manifests']['oms'] = str(Path(manifest['backup'] + '.manifest.json'))
    now = datetime.now(timezone.utc)
    edit(options, preflight, captured_at=now.isoformat(), expires_at=(now + timedelta(minutes=5)).isoformat())
    options.action = 'qualify'
    assert cli.qualify(options)['status'] == 'empty_oms_requires_explicit_provision'
    options.action = 'provision-oms'
    assert cli.write_offline(options, clock=lambda: now)['status'] == 'completed'
    options.action = 'apply'
    with pytest.raises(ValueError):
        cli.write_offline(options, clock=lambda: now)  # Old OMS backup/plan cannot authorize apply.
    manifest = ops().backup(stores['oms'], tmp_path / 'oms.after-provision.sqlite')
    preflight['backup_manifests']['oms'] = str(Path(manifest['backup'] + '.manifest.json'))
    now = datetime.now(timezone.utc)
    edit(options, preflight, captured_at=now.isoformat(), expires_at=(now + timedelta(minutes=5)).isoformat())
    options.action = 'plan'
    options.reviewed_plan_sha256 = cli.qualify(options)['reviewed_plan_sha256']
    options.action = 'apply'
    assert cli.write_offline(options, clock=lambda: now)['status'] == 'completed'


def test_busy_operator_lease_and_competing_writer_refuse_cleanly(tmp_path, monkeypatch):
    import fcntl
    options, preflight, stores, now = fixture(tmp_path, monkeypatch)
    before = snapshots(stores)
    with Path(preflight['lease_file']).open('rb') as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            cli.write_offline(options, clock=lambda: now)
    with sqlite3.connect(stores['ai_spend']) as writer:
        writer.execute('BEGIN IMMEDIATE')
        with pytest.raises(sqlite3.OperationalError):
            cli.write_offline(options, clock=lambda: now)
        writer.rollback()
    assert snapshots(stores) == before


def test_restore_drill_reuses_verified_backup_without_overwriting_source(tmp_path, monkeypatch):
    options, preflight, stores, _ = fixture(tmp_path, monkeypatch)
    before = snapshots(stores)
    manifest = json.loads(Path(preflight['backup_manifests']['ledger']).read_text())
    restored = tmp_path / 'isolated-restore.sqlite'
    ops().backup(Path(manifest['backup']), restored)
    with read_database(restored) as db:
        assert store_snapshot_sha256(db) == before['ledger']
    assert snapshots(stores) == before
    assert cli.qualify(SimpleNamespace(**{**vars(options), 'action': 'plan'}))['status'] == 'read_only_plan'


@pytest.mark.parametrize('change', ['nonpaper', 'observation', 'duplicate', 'unknown_field'])
def test_catalog_identity_scope_refused_before_writes(tmp_path, monkeypatch, change):
    options, _, stores, now = fixture(tmp_path, monkeypatch)
    path = Path(options.catalog)
    value = json.loads(path.read_text())
    if change == 'nonpaper': value['instruments'][0]['market'] = 'USA'
    if change == 'observation': value['instruments'][0]['tradable'] = False
    if change == 'duplicate': value['instruments'] *= 2
    if change == 'unknown_field': value['instruments'][0]['secret_token'] = 'secret-fixture'
    private_json(path, value)
    before = snapshots(stores)
    with pytest.raises(ValueError):
        cli.write_offline(options, clock=lambda: now)
    assert snapshots(stores) == before


def test_error_report_never_echoes_pasted_secret_or_private_paths(capsys):
    assert cli.main(['--accidental-secret', 'secret-token-marker']) == 2
    output = capsys.readouterr().out
    assert 'secret-token-marker' not in output
    assert 'refused' in output


def test_compose_forwards_selector_with_default_off_only_to_paper_engine():
    compose = yaml.safe_load((ROOT / 'deploy/docker-compose.yml').read_text())
    engine = compose['services']['pramana-ghost']['environment']
    assert engine['PRAMANA_PAPER_OPPORTUNITY_SELECTION'] == '${PRAMANA_PAPER_OPPORTUNITY_SELECTION:-false}'
    assert engine['TRADING_LIVE_MONEY_ACTIVE'] == 'false'
    for name, service in compose['services'].items():
        if name != 'pramana-ghost':
            assert 'PRAMANA_PAPER_OPPORTUNITY_SELECTION' not in service.get('environment', {})
