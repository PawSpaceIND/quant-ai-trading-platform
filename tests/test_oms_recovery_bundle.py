"""Offline synthetic bundles; no real account recovery, process stop or activation."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from quant_ai.domain.models import AssetClass, Instrument, InstrumentBoundOrderIntent, Market, Side
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.governance.runtime_identity import configure_runtime_identity
from quant_ai.operations import recovery_bundle as bundle
from quant_ai.orders.oms import DurableOms
from quant_ai.orders.state import OrderState

NOW = datetime(2026, 9, 15, 6, tzinfo=timezone.utc)


def bound_fixture(tmp_path, *, pending=False):
    root = tmp_path / "source"
    root.mkdir()
    broker = PaperBrokerService(root / "ledger", slippage_bps=Decimal(0))
    oms = DurableOms(root / "orders.sqlite")
    instrument = Instrument("INFY", Market.INDIA, AssetClass.EQUITY, "INR", "NSE")
    configure_runtime_identity(broker, (instrument,), "pilot", "bound_v1", oms.path, oms=oms)
    order = InstrumentBoundOrderIntent("INFY", Market.INDIA, Side.BUY, 2,
        Decimal(100), "synthetic", tenant_id="pilot", instrument=instrument)
    saved = oms.create(order, decision_id="synthetic-decision", now=NOW)
    oms.transition(saved.client_order_id, OrderState.RISK_APPROVED, now=NOW)
    oms.transition(saved.client_order_id, OrderState.SUBMITTED, now=NOW)
    fill = broker.buy(order)
    if not pending:
        oms.fill(saved.client_order_id, fill_id=fill.order_id, quantity=2,
                 price=fill.average_price, broker_order_id=fill.order_id,
                 now=broker.ledger_entries("pilot")[0].created_at)
    broker.close()
    oms.close()
    with sqlite3.connect(root / "console") as db:
        db.executescript("CREATE TABLE preferences(id INTEGER); CREATE TABLE conversations(id INTEGER); CREATE TABLE audit(id INTEGER);")
    db.close()
    for name in ("proofs", "reviews"):
        (root / name).mkdir()
    (root / "proofs" / "fill.json").write_text(json.dumps({"order_id": fill.order_id, "fixture": True}))
    (root / "directives").write_text('{"fixture":true}')
    (root / "halt").write_text("retain operator halt")
    spec = {"revision": "a" * 40, "tenant": "pilot",
            "sources": {name: str(root / name) for name in bundle.KINDS}}
    return spec, root / "orders.sqlite", saved.client_order_id


def test_bound_account_cannot_create_recovery_bundle_without_its_oms(tmp_path):
    spec, _, _ = bound_fixture(tmp_path)
    with pytest.raises(ValueError, match="Recovery OMS required"):
        bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    assert not (tmp_path / "backup").exists()


def test_bound_bundle_restores_order_history_but_never_rebinds_or_activates(tmp_path):
    spec, oms_path, cid = bound_fixture(tmp_path)
    spec["oms"] = str(oms_path)
    before = oms_path.read_bytes()
    manifest = bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    assert manifest["schema"] == 3 and "oms" in manifest["files"]
    report = bundle.restore(tmp_path / "backup", tmp_path / "restored",
                            manifest_sha256=manifest["manifestSha256"])
    assert report["status"] == "restored"
    assert report["orderRecovery"]["status"] == "captured_state_verified"
    assert report["orderRecovery"]["pathRebindRequired"] is True
    assert report["orderRecovery"]["liveExecutionAuthorized"] is False
    assert oms_path.read_bytes() == before
    with DurableOms(tmp_path / "restored" / "oms", read_only=True) as restored:
        assert restored.verify(cid)["verified"]
    assert (tmp_path / "restored" / "halt").read_text() == "retain operator halt"


def test_pending_orders_survive_capture_and_keep_restore_discrepant(tmp_path):
    spec, oms_path, cid = bound_fixture(tmp_path, pending=True)
    spec["oms"] = str(oms_path)
    manifest = bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    report = bundle.restore(tmp_path / "backup", tmp_path / "restored",
                            manifest_sha256=manifest["manifestSha256"])
    assert report["status"] == "discrepancy"
    assert report["orderRecovery"]["pendingOrders"] == 1
    assert report["orderRecovery"]["unmatchedLedgerFills"] == 1
    with DurableOms(tmp_path / "restored" / "oms", read_only=True) as restored:
        assert restored.get(cid).state is OrderState.SUBMITTED


def selected(tmp_path, *, pending=False):
    spec, path, cid = bound_fixture(tmp_path, pending=pending)
    spec["oms"] = str(path)
    return spec, path, cid


def change_database(path, table, sql):
    with sqlite3.connect(path) as db:
        for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,)).fetchall():
            assert name.replace("_", "").isalnum()
            db.execute(f'DROP TRIGGER "{name}"')
        db.execute(sql)
    db.close()


def write_manifest(root, payload):
    file = root / "manifest.json"
    file.write_text(json.dumps(payload))
    return bundle.digest(file)


@pytest.mark.parametrize("role", ["oms", "ledger"])
def test_changed_source_during_capture_aborts_and_keeps_original(tmp_path, monkeypatch, role):
    spec, oms_path, _ = selected(tmp_path)
    original = bundle.sqlite_backup
    def changing(source, target):
        original(source, target)
        if target.name == "oms":
            chosen = oms_path if role == "oms" else Path(spec["sources"]["ledger"])
            with sqlite3.connect(chosen) as db:
                db.execute("CREATE TABLE synthetic_concurrent_write(id INTEGER)")
            db.close()
    monkeypatch.setattr(bundle, "sqlite_backup", changing)
    with pytest.raises(ValueError, match="Source changed"):
        bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    assert oms_path.exists() and not (tmp_path / "backup").exists()


@pytest.mark.parametrize("defect", ["hash", "projection", "orphan_event", "schema", "missing_table"])
def test_invalid_oms_history_is_not_packaged_as_verified(tmp_path, defect):
    spec, path, _ = selected(tmp_path)
    table, sql = {
        "hash": ("oms_events", "UPDATE oms_events SET event_hash='bad' WHERE sequence=1"),
        "projection": ("oms_orders", "UPDATE oms_orders SET filled_quantity=0"),
        "orphan_event": ("oms_events", "UPDATE oms_events SET client_order_id='OMS-orphan' WHERE sequence=1"),
        "schema": ("oms_meta", "UPDATE oms_meta SET version=99"),
        "missing_table": ("oms_replacements", "DROP TABLE oms_replacements"),
    }[defect]
    change_database(path, table, sql)
    before = path.read_bytes()
    with pytest.raises((ValueError, sqlite3.Error)):
        bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    assert not (tmp_path / "backup").exists() and path.read_bytes() == before


def test_same_path_empty_replacement_preserves_discrepancy(tmp_path):
    spec, path, _ = selected(tmp_path)
    path.unlink()
    with DurableOms(path):
        pass
    manifest = bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    result = bundle.restore(tmp_path / "backup", tmp_path / "restored", manifest_sha256=manifest["manifestSha256"])
    assert result["status"] == "discrepancy"
    assert result["orderRecovery"]["unmatchedLedgerFills"] == 1


def test_wrong_oms_path_never_satisfies_saved_storage_pin(tmp_path):
    spec, _, _ = selected(tmp_path)
    other = tmp_path / "unrelated.sqlite"
    with DurableOms(other):
        pass
    spec["oms"] = str(other)
    with pytest.raises(ValueError, match="source does not match"):
        bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    assert not (tmp_path / "backup").exists()


@pytest.mark.parametrize("schema", [1, 2])
def test_older_bound_bundle_without_oms_refuses_restore_even_with_retained_hash(tmp_path, schema):
    spec, _, _ = selected(tmp_path)
    manifest = bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    manifest.pop("manifestSha256")
    manifest["schema"] = schema
    manifest.pop("orderState")
    manifest["files"].pop("oms")
    (tmp_path / "backup" / "oms").unlink()
    checksum = write_manifest(tmp_path / "backup", manifest)
    with pytest.raises(ValueError, match="Recovery OMS required"):
        bundle.restore(tmp_path / "backup", tmp_path / "restored", manifest_sha256=checksum)
    assert not (tmp_path / "restored").exists()


@pytest.mark.parametrize("change", ["missing_file", "bad_file_hash", "changed_verification", "changed_pin"])
def test_restore_detects_incomplete_or_altered_order_state(tmp_path, change):
    spec, _, _ = selected(tmp_path)
    manifest = bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    checksum = manifest.pop("manifestSha256")
    if change == "missing_file":
        (tmp_path / "backup" / "oms").unlink()
    elif change == "bad_file_hash":
        with (tmp_path / "backup" / "oms").open("ab") as file:
            file.write(b"synthetic-corruption")
    else:
        if change == "changed_verification":
            manifest["orderState"]["verification"]["orders"] = 0
        else:
            manifest["orderState"]["sourcePathSha256"] = "f" * 64
        checksum = write_manifest(tmp_path / "backup", manifest)
    with pytest.raises(ValueError):
        bundle.restore(tmp_path / "backup", tmp_path / "restored", manifest_sha256=checksum)
    assert not (tmp_path / "restored").exists()


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_source_oms_alias_is_refused(tmp_path, kind):
    import os
    spec, path, _ = selected(tmp_path)
    alias = tmp_path / "alias.sqlite"
    if kind == "symlink":
        alias.symlink_to(path)
    else:
        os.link(path, alias)
    spec["oms"] = str(alias)
    with pytest.raises(ValueError, match="unaliased"):
        bundle.create(spec, tmp_path / "backup", writers_stopped=True)


@pytest.mark.parametrize("declaration", [False, "true", 1, None])
def test_stopped_writer_declaration_must_be_literal_true(tmp_path, declaration):
    spec, _, _ = selected(tmp_path)
    with pytest.raises(ValueError, match="Stop all writers"):
        bundle.create(spec, tmp_path / "backup", writers_stopped=declaration)
    assert not (tmp_path / "backup").exists()


def test_oms_inspection_does_not_create_missing_database_or_migrate_existing_one(tmp_path, monkeypatch):
    missing = tmp_path / "absent" / "orders.sqlite"
    with pytest.raises(ValueError, match="existing"):
        DurableOms(missing, read_only=True)
    assert not missing.parent.exists()
    _, path, cid = selected(tmp_path)
    before = path.read_bytes()
    monkeypatch.setattr(DurableOms, "_schema", lambda *a: pytest.fail("inspection must not migrate"))
    with DurableOms(path, read_only=True) as oms:
        assert oms.verify(cid)["verified"]
        with pytest.raises(sqlite3.OperationalError):
            oms.db.execute("DELETE FROM oms_meta")
        with pytest.raises(sqlite3.OperationalError):
            oms.create(oms.get_intent(cid), decision_id="denied-write")
    assert path.read_bytes() == before


def test_existing_destination_and_original_pin_are_never_replaced(tmp_path):
    spec, path, _ = selected(tmp_path)
    manifest = bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    before = {p.name: p.read_bytes() for p in (path, Path(spec["sources"]["ledger"]))}
    dest = tmp_path / "restored"
    dest.mkdir()
    sentinel = dest / "keep"
    sentinel.write_text("existing user state")
    with pytest.raises(FileExistsError):
        bundle.restore(tmp_path / "backup", dest, manifest_sha256=manifest["manifestSha256"])
    assert sentinel.read_text() == "existing user state"
    assert before == {p.name: p.read_bytes() for p in (path, Path(spec["sources"]["ledger"]))}


def test_bare_protective_outbox_cannot_hide_an_unmatched_ordinary_fill(tmp_path):
    spec, _, _ = selected(tmp_path, pending=True)
    ledger = Path(spec["sources"]["ledger"])
    with sqlite3.connect(ledger) as db:
        order_id = db.execute("SELECT order_id FROM paper_ledger").fetchone()[0]
        db.execute("INSERT INTO paper_protective_fill_outbox VALUES (?,?,?)", (order_id, "pilot", "f" * 64))
    db.close()
    with pytest.raises(ValueError, match="protective evidence inventory"):
        bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    assert not (tmp_path / "backup").exists()


def test_backup_and_restore_never_invoke_recovery_or_trade_actions(tmp_path, monkeypatch):
    from quant_ai.orders.paper_recovery import PaperOmsRecovery
    spec, _, _ = selected(tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail("offline bundle must not submit/recover/clear")
    monkeypatch.setattr(PaperOmsRecovery, "recover", forbidden)
    monkeypatch.setattr(PaperBrokerService, "buy", forbidden)
    monkeypatch.setattr(PaperBrokerService, "sell", forbidden)
    manifest = bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    report = bundle.restore(tmp_path / "backup", tmp_path / "restored", manifest_sha256=manifest["manifestSha256"])
    assert report["activation"].startswith("none")


@pytest.mark.parametrize("missing_audit", [False, True])
def test_recovered_version_two_history_is_captured_or_refused_without_repair(tmp_path, monkeypatch, missing_audit):
    from test_paper_oms_recovery import identifiers, interrupted_runner, recovery_for
    from test_runtime_contract_mode import close
    root = tmp_path / "source"
    root.mkdir()
    runner = interrupted_runner(root, monkeypatch)
    _, cid = identifiers(runner)
    service = recovery_for(runner)
    plan = service.inspect(cid, tenant_id="pilot")
    assert service.recover(cid, tenant_id="pilot", reviewed_plan_sha256=plan.sha256,
                           reviewer="synthetic-reviewer").status == "MATCHED"
    close(runner)
    with sqlite3.connect(root / "console") as db:
        db.executescript("CREATE TABLE preferences(id INTEGER); CREATE TABLE conversations(id INTEGER); CREATE TABLE audit(id INTEGER);")
    db.close()
    for name in ("proofs", "reviews"):
        (root / name).mkdir(exist_ok=True)
    (root / "directives").write_text('{"fixture":true}')
    (root / "halt").write_text("remain halted")
    spec = {"revision": "a" * 40, "tenant": "pilot", "oms": str(root / "oms.sqlite"),
            "sources": {name: str(root / ("ledger.db" if name == "ledger" else name)) for name in bundle.KINDS}}
    if missing_audit:
        change_database(root / "oms.sqlite", "oms_paper_recoveries", "DROP TABLE oms_paper_recoveries")
        with pytest.raises(ValueError, match="schema inventory"):
            bundle.create(spec, tmp_path / "backup", writers_stopped=True)
        assert not (tmp_path / "backup").exists()
        return
    manifest = bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    result = bundle.restore(tmp_path / "backup", tmp_path / "restored", manifest_sha256=manifest["manifestSha256"])
    assert result["status"] == "restored" and result["orderRecovery"]["omsVersion"] == 2
    with DurableOms(tmp_path / "restored" / "oms", read_only=True) as oms:
        assert oms.verify(cid)["verified"]
        assert oms.db.execute("SELECT COUNT(*) FROM oms_paper_recoveries").fetchone()[0] == 1
    with sqlite3.connect(tmp_path / "restored" / "ledger") as db:
        assert db.execute("SELECT kill_switch_engaged FROM risk_control_state WHERE tenant_id='pilot'").fetchone()[0] == 1
    db.close()


def test_committed_wal_frames_are_captured_with_the_oms(tmp_path):
    spec, path, cid = selected(tmp_path)
    with DurableOms(path) as writer:
        order = writer.get_intent(cid)
        extra = writer.create(order, decision_id="in-wal", now=NOW)
        writer.transition(extra.client_order_id, OrderState.CANCELLED, now=NOW)
        assert Path(str(path) + "-wal").stat().st_size > 0
        manifest = bundle.create(spec, tmp_path / "backup", writers_stopped=True)
        result = bundle.restore(tmp_path / "backup", tmp_path / "restored", manifest_sha256=manifest["manifestSha256"])
        assert result["orderRecovery"]["orders"] == 2
        with DurableOms(tmp_path / "restored" / "oms", read_only=True) as restored:
            assert restored.get(extra.client_order_id).state is OrderState.CANCELLED
            assert restored.verify(extra.client_order_id)["verified"]


def test_readonly_uri_still_prevents_writes_when_query_only_is_disabled(tmp_path):
    _, path, _ = selected(tmp_path)
    before = path.read_bytes()
    with DurableOms(path, read_only=True) as reader:
        reader.db.execute("PRAGMA query_only=OFF")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.db.execute("UPDATE oms_meta SET version=77")
    assert path.read_bytes() == before


def test_valid_protective_exit_is_preserved_but_not_fabricated_as_an_oms_order(tmp_path):
    from quant_ai.execution.protective_exits import ProtectiveExitEngine
    spec, _, _ = selected(tmp_path)
    broker = PaperBrokerService(spec["sources"]["ledger"])
    with broker._connection as db:
        db.execute("UPDATE paper_positions SET stop_price='95'")
        db.execute("UPDATE paper_ledger SET stop_price='95'")
    assert ProtectiveExitEngine(broker, lambda _: Decimal(90), tenant_id="pilot").evaluate()[0].filled
    broker.close()
    manifest = bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    report = bundle.restore(tmp_path / "backup", tmp_path / "restored", manifest_sha256=manifest["manifestSha256"])
    assert report["status"] == "restored"
    assert report["orderRecovery"]["orders"] == 1
    assert report["orderRecovery"]["protectiveFillsExcluded"] == 1
    assert report["proofCoverage"]["ledgerProtectionRecords"] == 1


@pytest.mark.parametrize("column", ["requested_quantity", "filled_quantity", "last_event_sequence"])
def test_fractional_stored_projection_is_not_silently_truncated_by_replay(tmp_path, column):
    spec, path, _ = selected(tmp_path)
    change_database(path, "oms_orders", f"UPDATE oms_orders SET {column}={column}+0.5")
    with pytest.raises(ValueError, match="Recovery OMS projection integers"):
        bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    assert not (tmp_path / "backup").exists()


@pytest.mark.parametrize("kind", ["replacement", "unused_audit"])
def test_unreferenced_order_history_cannot_enter_verified_bundle(tmp_path, kind):
    spec, path, cid = selected(tmp_path)
    with sqlite3.connect(path) as db:
        if kind == "replacement":
            db.execute("INSERT INTO oms_replacements VALUES (?,?,?,?)", ("OMS-orphan", cid, "synthetic", NOW.isoformat()))
        else:
            db.execute("UPDATE oms_meta SET version=2")
            db.execute("CREATE TABLE oms_paper_recoveries(client_order_id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL)")
            db.execute("INSERT INTO oms_paper_recoveries VALUES (?,?,?,?)", (cid, "pilot", "{}", "f" * 64))
    db.close()
    with pytest.raises(ValueError, match="Recovery OMS orphaned"):
        bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    assert not (tmp_path / "backup").exists()
