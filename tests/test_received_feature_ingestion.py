"""Synthetic, credential-free receipt/feature-store and downstream integration checks."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from quant_ai.features.store import FeatureObservation, FeatureRequirement, PointInTimeFeatureStore
from quant_ai.learning import ingestion as m
from quant_ai.learning.contracts import AccessPlane, KnowledgeCategory, RightsStatus, SourceGrant

T = datetime(2026, 9, 1, 10, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def grant(**changes):
    return replace(SourceGrant("recorded", "synthetic-test", frozenset({KnowledgeCategory.MARKET,
        KnowledgeCategory.BROKER}), frozenset({AccessPlane.TRAINING}), True, RightsStatus.INTERNAL), **changes)


def record(**changes):
    return {"record_id": "one", "subject": "INDIA:NSE:TEST:INR", "feature": "signal",
            "value": "1", "observed_at": T.isoformat(), "schema_id": "feature:v1",
            "category": "MARKET", **changes}


def batch(records=None, **changes):
    return m.canonical({"schema": m.BATCH_SCHEMA, "source_id": "recorded", "batch_id": "batch-1",
                        "records": [record()] if records is None else records, **changes})


def send(store, raw=None, at=T, **kwargs):
    return store.receive(batch() if raw is None else raw, source_grants=(grant(),), clock=lambda: at, **kwargs)


def counts(store):
    return tuple(store.db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                 for table in ("feature_observations", "received_feature_records", "received_feature_batches"))


def refuses(call, reason):
    try:
        call()
    except Exception as error:  # noqa: BLE001 - require the precise refusal, not incidental errors
        assert type(error) is ValueError and str(error) == "feature_ingestion_" + reason, (type(error).__name__, str(error))
    else:
        raise AssertionError("expected feature_ingestion_" + reason)


def alter(store, table, sql, params=()):
    with store.db:
        store.db.execute(f"DROP TRIGGER IF EXISTS {table}_update_blocked")
        store.db.execute(f"DROP TRIGGER IF EXISTS {table}_delete_blocked")
        store.db.execute(sql, params)


def test_import_uses_actual_receipt_time_not_historical_observation_time(tmp_path):
    with m.ReceivedFeatureStore(tmp_path / "source.db", tenant_id="ghost") as store:
        now = T + timedelta(days=2)
        send(store, at=now)
        row = store.db.execute("SELECT * FROM feature_observations").fetchone()
        assert row["observed_at"] == T.isoformat()
        assert row["available_at"] == now.isoformat()
        req = (FeatureRequirement("signal", 999999, frozenset({"recorded"}), "feature:v1"),)
        with pytest.raises(ValueError, match="feature_unavailable"):
            store.snapshot(subject=record()["subject"], as_of=T + timedelta(seconds=1), requirements=req)
        assert store.snapshot(subject=record()["subject"], as_of=now, requirements=req).values() == {"signal": Decimal(1)}


def test_exact_retry_and_restart_retain_first_receipt_and_one_value(tmp_path):
    path = tmp_path / "source.db"
    with m.ReceivedFeatureStore(path, tenant_id="ghost") as store:
        first = send(store)
        assert first["new_observations"] == 1 and not first["duplicate_batch"]
        again = send(store, at=T + timedelta(hours=1))
        assert again["duplicate_batch"] and again["received_at"] == first["received_at"]
        assert counts(store) == (1, 1, 1)
    with m.ReceivedFeatureStore(path, tenant_id="ghost") as store:
        assert send(store, at=T + timedelta(days=1))["duplicate_batch"]
        assert counts(store) == (1, 1, 1)


def test_same_record_in_new_batch_preserves_first_known_time(tmp_path):
    with m.ReceivedFeatureStore(tmp_path / "source.db", tenant_id="ghost") as store:
        send(store)
        result = send(store, batch(batch_id="second"), at=T + timedelta(hours=1))
        assert result["new_observations"] == 0 and not result["duplicate_batch"]
        assert counts(store) == (1, 1, 2)
        assert store.db.execute("SELECT available_at FROM feature_observations").fetchone()[0] == T.isoformat()


@pytest.mark.parametrize("change", ["payload", "grant"])
def test_batch_identity_conflict_never_overwrites_receipt(tmp_path, change):
    with m.ReceivedFeatureStore(tmp_path / "source.db", tenant_id="ghost") as store:
        send(store)
        raw = batch([record(value="2")]) if change == "payload" else batch()
        selected = grant(provider="other") if change == "grant" else grant()
        refuses(lambda: store.receive(raw, source_grants=(selected,), clock=lambda: T), "batch_identity_reused")
        assert counts(store) == (1, 1, 1)


def test_changed_record_in_later_batch_is_refused_atomically(tmp_path):
    with m.ReceivedFeatureStore(tmp_path / "source.db", tenant_id="ghost") as store:
        send(store)
        raw = batch([record(record_id="new", feature="other"), record(value="2")], batch_id="second")
        refuses(lambda: send(store, raw), "new_source_record_changed")
        assert counts(store) == (1, 1, 1)


@pytest.mark.parametrize("case,reason", [
    ("schema", "batch_schema"), ("extra", "batch_schema"), ("empty", "batch_row_bound"),
    ("duplicate", "duplicate_source_record"), ("availability", "record_schema"),
    ("future", "future_observation"), ("category", "category_not_permitted"),
])
def test_invalid_batch_has_specific_refusal_before_any_feature_write(tmp_path, case, reason):
    data = json.loads(batch())
    if case == "schema": data["schema"] = "wrong"
    if case == "extra": data["token"] = "not-a-real-token"
    if case == "empty": data["records"] = []
    if case == "duplicate": data["records"] *= 2
    if case == "availability": data["records"][0]["available_at"] = T.isoformat()
    if case == "future": data["records"][0]["observed_at"] = (T + timedelta(microseconds=1)).isoformat()
    if case == "category": data["records"][0]["category"] = "NEWS"
    with m.ReceivedFeatureStore(tmp_path / "source.db", tenant_id="ghost") as store:
        refuses(lambda: send(store, m.canonical(data)), reason)
        assert counts(store) == (0, 0, 0)


@pytest.mark.parametrize("changes", [{"point_in_time": False}, {"rights_status": RightsStatus.UNVERIFIED},
    {"source_id": "other"}, {"planes": frozenset({AccessPlane.RESEARCH})}])
def test_source_permissions_refuse_before_feature_writes(tmp_path, changes):
    with m.ReceivedFeatureStore(tmp_path / "source.db", tenant_id="ghost") as store:
        refuses(lambda: store.receive(batch(), source_grants=(grant(**changes),), clock=lambda: T), "source_not_permitted")
        assert counts(store) == (0, 0, 0)


def test_typed_input_and_single_source_contract(tmp_path):
    refuses(lambda: m._prepare(batch(), (), T), "single_typed_grant")
    with m.ReceivedFeatureStore(tmp_path / "source.db", tenant_id="ghost") as store:
        refuses(lambda: store.receive(batch(), source_grants=["wrong"], clock=lambda: T), "frozen_inputs")


def test_clock_cannot_move_behind_saved_receipt(tmp_path):
    with m.ReceivedFeatureStore(tmp_path / "source.db", tenant_id="ghost") as store:
        send(store, at=T + timedelta(seconds=1))
        refuses(lambda: send(store), "clock_regressed")
        assert counts(store) == (1, 1, 1)


def test_clock_callback_cannot_replace_grant_after_snapshot(tmp_path):
    selected = grant()
    def clock():
        object.__setattr__(selected, "source_id", "other")
        return T
    with m.ReceivedFeatureStore(tmp_path / "source.db", tenant_id="ghost") as store:
        store.receive(batch(), source_grants=[selected], clock=clock)
        assert counts(store) == (1, 1, 1)


def test_legacy_store_is_not_adopted_or_changed(tmp_path):
    path = tmp_path / "old.db"
    with PointInTimeFeatureStore(path) as old:
        old.append(FeatureObservation("legacy", "subject", "signal", Decimal(1), T, T, "recorded", "feature:v1"))
    before = path.read_bytes()
    with pytest.raises(sqlite3.OperationalError, match="received_feature_meta"):
        m.ReceivedFeatureStore(path, tenant_id="ghost")
    assert path.read_bytes() == before


def test_wrong_tenant_refuses_existing_store(tmp_path):
    path = tmp_path / "source.db"
    with m.ReceivedFeatureStore(path, tenant_id="ghost") as store: send(store)
    refuses(lambda: m.ReceivedFeatureStore(path, tenant_id="other"), "tenant_binding")


def test_direct_append_without_receipt_is_refused(tmp_path):
    with m.ReceivedFeatureStore(tmp_path / "source.db", tenant_id="ghost") as store:
        refuses(lambda: store.append(None), "receipt_required")
        assert counts(store) == (0, 0, 0)


def test_replaced_store_refuses_on_existing_handle(tmp_path):
    path = tmp_path / "source.db"
    with m.ReceivedFeatureStore(path, tenant_id="ghost") as store:
        path.rename(tmp_path / "renamed.db")
        path.touch(mode=0o600)
        refuses(lambda: send(store), "selected_store_replaced")


def test_output_parent_requires_private_directory(tmp_path):
    tmp_path.chmod(0o755)
    try:
        refuses(lambda: m.ReceivedFeatureStore(tmp_path / "source.db", tenant_id="ghost"), "private_directory")
    finally: tmp_path.chmod(0o700)


@pytest.mark.parametrize("kind", ["public", "symlink", "hardlink"])
def test_source_file_privacy(tmp_path, kind):
    path = tmp_path / "source.db"
    with m.ReceivedFeatureStore(path, tenant_id="ghost"): pass
    if kind == "public": path.chmod(0o644)
    if kind == "symlink":
        path.rename(tmp_path / "target.db"); path.symlink_to(tmp_path / "target.db")
    if kind == "hardlink": os.link(path, tmp_path / "alias.db")
    with pytest.raises(ValueError, match="private_source"):
        m.ReceivedFeatureStore(path, tenant_id="ghost")


@pytest.mark.parametrize("table", ["feature_observations", "received_feature_records", "received_feature_batches"])
def test_write_failure_rolls_back_every_row(tmp_path, table):
    with m.ReceivedFeatureStore(tmp_path / "source.db", tenant_id="ghost") as store:
        store.db.execute(f"CREATE TRIGGER injected_failure BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT,'synthetic interruption'); END")
        expected = ValueError if table == "feature_observations" else sqlite3.IntegrityError
        message = "duplicate_feature_revision_identity" if table == "feature_observations" else "synthetic interruption"
        with pytest.raises(expected, match=message): send(store)
        assert counts(store) == (0, 0, 0)


@pytest.mark.parametrize("table", ["received_feature_meta", "received_feature_records", "received_feature_batches"])
@pytest.mark.parametrize("verb", ["UPDATE", "DELETE"])
def test_receipt_tables_are_append_only(tmp_path, table, verb):
    with m.ReceivedFeatureStore(tmp_path / "source.db", tenant_id="ghost") as store:
        send(store)
        column = "tenant_id" if table.endswith("meta") else "payload"
        sql = f"UPDATE {table} SET {column}={column}" if verb == "UPDATE" else f"DELETE FROM {table}"
        with pytest.raises(sqlite3.IntegrityError, match="append-only"), store.db:
            store.db.execute(sql)
        assert counts(store) == (1, 1, 1)


def test_same_instance_threads_record_one_receipt(tmp_path):
    with m.ReceivedFeatureStore(tmp_path / "source.db", tenant_id="ghost") as store:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: send(store), range(2)))
        assert sum(x["new_observations"] for x in results) == 1
        assert sorted(x["duplicate_batch"] for x in results) == [False, True]
        assert counts(store) == (1, 1, 1)


def test_independent_connections_record_one_receipt(tmp_path):
    path = tmp_path / "source.db"
    with m.ReceivedFeatureStore(path, tenant_id="ghost"): pass
    def job(_):
        with m.ReceivedFeatureStore(path, tenant_id="ghost") as store: return send(store)
    with ThreadPoolExecutor(max_workers=2) as pool: results = list(pool.map(job, range(2)))
    assert sum(x["new_observations"] for x in results) == 1
    with m.ReceivedFeatureStore(path, tenant_id="ghost") as store: assert counts(store) == (1, 1, 1)


@pytest.mark.parametrize("after_commit", [False, True])
def test_process_death_never_leaves_partial_feature_receipt(tmp_path, after_commit):
    path = tmp_path / "source.db"
    with m.ReceivedFeatureStore(path, tenant_id="ghost"): pass
    child = """import os, sys
from test_received_feature_ingestion import batch, grant, T
from quant_ai.learning.ingestion import ReceivedFeatureStore
with ReceivedFeatureStore(sys.argv[1], tenant_id='ghost') as store:
    if sys.argv[2] == 'False':
        original = store._insert_observation
        def interrupted(item):
            original(item)
            os._exit(23)
        store._insert_observation = interrupted
    store.receive(batch(),source_grants=(grant(),),clock=lambda:T)
os._exit(23)
"""
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), str(ROOT / "tests")]),
           "TRADING_LIVE_MONEY_ACTIVE": "false"}
    done = subprocess.run([sys.executable, "-B", "-c", child, str(path), str(after_commit)],
                          capture_output=True, text=True, env=env, timeout=15, check=False)
    assert done.returncode == 23, done.stderr
    with m.ReceivedFeatureStore(path, tenant_id="ghost") as store:
        assert counts(store) == ((1, 1, 1) if after_commit else (0, 0, 0))
        send(store)
        assert counts(store) == (1, 1, 1)


def test_paper_only_receipt(tmp_path, monkeypatch):
    with m.ReceivedFeatureStore(tmp_path / "source.db", tenant_id="ghost") as store:
        monkeypatch.setenv("TRADING_LIVE_MONEY_ACTIVE", "true")
        refuses(lambda: send(store), "paper_only")
        assert counts(store) == (0, 0, 0)


def test_reopening_uses_one_snapshot_during_concurrent_receipt(tmp_path, monkeypatch):
    path = tmp_path / "source.db"
    with m.ReceivedFeatureStore(path, tenant_id="ghost") as writer:
        send(writer)
        original = sqlite3.connect
        fired = []
        def connect(*args, **kwargs):
            db = original(*args, **kwargs)
            if isinstance(args[0], str) and "?mode=ro" in args[0]:
                def trace(sql):
                    if "SELECT * FROM feature_observations LIMIT" in sql and not fired:
                        fired.append(True)
                        send(writer, batch([record(record_id="two", observed_at=(T + timedelta(seconds=2)).isoformat())], batch_id="two"), at=T + timedelta(seconds=2))
                db.set_trace_callback(trace)
            return db
        monkeypatch.setattr(sqlite3, "connect", connect)
        with m.ReceivedFeatureStore(path, tenant_id="ghost") as opened:
            assert counts(opened) == (2, 2, 2)
        assert fired


def test_same_connection_snapshot_cannot_see_uncommitted_feature(tmp_path, monkeypatch):
    import threading
    with m.ReceivedFeatureStore(tmp_path / "source.db", tenant_id="ghost") as store:
        entered, release, read_started = threading.Event(), threading.Event(), threading.Event()
        original = store._insert_observation
        def pause(item):
            original(item); entered.set(); assert release.wait(5)
        monkeypatch.setattr(store, "_insert_observation", pause)
        def read():
            read_started.set()
            return store.snapshot(subject=record()["subject"], as_of=T,
                requirements=(FeatureRequirement("signal", 60),))
        with ThreadPoolExecutor(max_workers=2) as pool:
            write = pool.submit(send, store)
            try:
                assert entered.wait(5)
                reading = pool.submit(read)
                assert read_started.wait(5)
                try:
                    reading.result(timeout=0.05)
                    escaped = True
                except TimeoutError:
                    escaped = False
            finally:
                release.set()
            assert write.result()["new_observations"] == 1
            assert reading.result().values() == {"signal": Decimal(1)}
        assert not escaped, "Reader observed a feature before its receipt transaction committed"


def verify(store):
    with store.db:
        store.db.execute("BEGIN")
        return m.validate_received_features(store.db, tenant_id="ghost")


def change_receipt(store, sequence=1, **changes):
    row = dict(store.db.execute("SELECT * FROM received_feature_batches WHERE sequence=?", (sequence,)).fetchone())
    row.update(changes)
    if "sha256" not in changes:
        row["sha256"] = m._receipt_digest("ghost", row["source_id"], row["batch_id"],
            row["payload"], row["grant_payload"], row["received_at"])
    keys = [k for k in row if k != "sequence"]
    alter(store, "received_feature_batches", "UPDATE received_feature_batches SET " +
        ",".join(k + "=?" for k in keys) + " WHERE sequence=?", (*[row[k] for k in keys], sequence))


def test_validator_requires_stable_read_transaction(tmp_path):
    with m.ReceivedFeatureStore(tmp_path / "source.db", tenant_id="ghost") as store:
        refuses(lambda: m.validate_received_features(store.db, tenant_id="ghost"), "read_transaction_required")


@pytest.mark.parametrize("kind,reason", [
    ("digest", "receipt_digest"), ("identity", "receipt_identity"),
    ("coverage", "source_coverage"), ("missing", "record_lineage_missing"),
    ("record", "record_lineage_mismatch"),
])
def test_corrupted_saved_evidence_has_named_refusal(tmp_path, kind, reason):
    with m.ReceivedFeatureStore(tmp_path / "source.db", tenant_id="ghost") as store:
        send(store)
        if kind == "digest": change_receipt(store, sha256="0" * 64)
        if kind == "identity": change_receipt(store, source_id="other")
        if kind == "coverage": alter(store, "received_feature_records", "DELETE FROM received_feature_records")
        if kind == "missing": alter(store, "received_feature_records", "UPDATE received_feature_records SET observation_id='orphan'")
        if kind == "record": alter(store, "received_feature_records", "UPDATE received_feature_records SET payload='wrong'")
        refuses(lambda: verify(store), reason)


def test_retained_same_id_cannot_describe_two_values(tmp_path):
    with m.ReceivedFeatureStore(tmp_path / "source.db", tenant_id="ghost") as store:
        send(store); send(store, batch(batch_id="two"))
        change_receipt(store, sequence=2, payload=batch([record(value="2")], batch_id="two").decode())
        refuses(lambda: verify(store), "source_record_changed")


def test_retained_receipt_times_cannot_go_backwards(tmp_path):
    with m.ReceivedFeatureStore(tmp_path / "source.db", tenant_id="ghost") as store:
        send(store); send(store, batch(batch_id="two"))
        change_receipt(store, sequence=2, received_at=(T - timedelta(seconds=1)).isoformat())
        refuses(lambda: verify(store), "receipt_time_order")


@pytest.mark.parametrize("existing,constant,reason", [
    (True, "MAX_RECEIPTS", "receipt_bound"), (False, "MAX_RECEIPTS", "new_receipt_bound"),
    (True, "MAX_STORE_ROWS", "store_row_bound"), (False, "MAX_STORE_ROWS", "new_store_row_bound"),
])
def test_capacity_refusals_do_not_partially_write(tmp_path, monkeypatch, existing, constant, reason):
    with m.ReceivedFeatureStore(tmp_path / "source.db", tenant_id="ghost") as store:
        if existing: send(store)
        before = counts(store)
        monkeypatch.setattr(m, constant, 0)
        refuses(lambda: verify(store) if existing else send(store), reason)
        assert counts(store) == before


def test_sql_feature_hash_is_independently_checked(tmp_path):
    with m.ReceivedFeatureStore(tmp_path / "source.db", tenant_id="ghost") as store:
        send(store)
        alter(store, "feature_observations", "UPDATE feature_observations SET value='2'")
        with pytest.raises(ValueError, match="feature_observation_hash_mismatch"): verify(store)


def test_restored_table_hash_cannot_hide_changed_availability(tmp_path):
    from quant_ai.features.store import _sha
    with m.ReceivedFeatureStore(tmp_path / "source.db", tenant_id="ghost") as store:
        send(store, at=T + timedelta(hours=1))
        row = store.db.execute("SELECT * FROM feature_observations").fetchone()
        payload = FeatureObservation(row["observation_id"], row["subject"], row["feature"],
            Decimal(row["value"]), T, T, row["source_id"], row["schema_id"]).payload()
        alter(store, "feature_observations", "UPDATE feature_observations SET available_at=?,payload_sha256=?",
              (T.isoformat(), _sha(payload)))
        refuses(lambda: verify(store), "record_lineage_mismatch")


def test_utc_and_ist_observation_instants_match(tmp_path):
    from zoneinfo import ZoneInfo
    with m.ReceivedFeatureStore(tmp_path / "source.db", tenant_id="ghost") as store:
        send(store, batch([record(observed_at=T.astimezone(ZoneInfo("Asia/Kolkata")).isoformat())]))
        assert store.db.execute("SELECT observed_at FROM feature_observations").fetchone()[0] == T.isoformat()


def test_grant_payload_has_direct_size_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "MAX_GRANT_BYTES", 1)
    with m.ReceivedFeatureStore(tmp_path / "source.db", tenant_id="ghost") as store:
        refuses(lambda: send(store), "grant_payload_bound")
        assert counts(store) == (0, 0, 0)


@pytest.mark.parametrize("existing,reason", [(True, "receipt_byte_bound"), (False, "new_receipt_byte_bound")])
def test_cumulative_receipt_byte_limit_precedes_large_reads_and_writes(tmp_path, monkeypatch, existing, reason):
    with m.ReceivedFeatureStore(tmp_path / "source.db", tenant_id="ghost") as store:
        if existing: send(store)
        before = counts(store)
        monkeypatch.setattr(m, "MAX_RECEIPT_BYTES", 1)
        refuses(lambda: verify(store) if existing else send(store), reason)
        assert counts(store) == before


def test_new_source_revision_does_not_rewrite_old_snapshot(tmp_path):
    with m.ReceivedFeatureStore(tmp_path / "source.db", tenant_id="ghost") as store:
        send(store)
        later = T + timedelta(seconds=1)
        send(store, batch([record(record_id="revision-two", value="2")], batch_id="two"), at=later)
        requirements = (FeatureRequirement("signal", 60),)
        assert store.snapshot(subject=record()["subject"], as_of=T, requirements=requirements).values() == {"signal": Decimal(1)}
        assert store.snapshot(subject=record()["subject"], as_of=later, requirements=requirements).values() == {"signal": Decimal(2)}


def test_retry_rechecks_selected_file_before_reporting_success(tmp_path):
    path = tmp_path / "source.db"
    with m.ReceivedFeatureStore(path, tenant_id="ghost") as store:
        send(store)
        changed = []
        def trace(sql):
            if "SELECT * FROM received_feature_batches WHERE source_id=" in sql and not changed:
                changed.append(True)
                path.rename(tmp_path / "old.db")
                path.touch(mode=0o600)
        store.db.set_trace_callback(trace)
        refuses(lambda: send(store), "selected_store_replaced")
        assert changed
