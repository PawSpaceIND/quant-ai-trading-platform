"""Receive numeric observations with local first-known time, for shadow research only.

No provider, broker, model promotion or historical availability override. Input
values and source permissions remain declarations; receipts are local evidence.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

from quant_ai.features.store import FeatureObservation, PointInTimeFeatureStore
from quant_ai.learning.contracts import AccessPlane, KnowledgeCategory, RightsStatus, SourceGrant
from quant_ai.learning.feature_dataset import _id, _number, _private, _time, canonical, decode

BATCH_SCHEMA = "pramana.numeric_observation_batch.v1"
STORE_SCHEMA = "pramana.received_features.v1"
MAX_BATCH_BYTES = 1_000_000
MAX_BATCH_ROWS = 1000
MAX_STORE_ROWS = 100_000
MAX_RECEIPTS = 10_000
MAX_GRANT_BYTES = 65_536
MAX_RECEIPT_BYTES = 32_000_000
RECORD_FIELDS = {"record_id", "subject", "feature", "value", "observed_at", "schema_id", "category"}


def _require(condition, reason):
    if not condition:
        raise ValueError("feature_ingestion_" + reason)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _grant_payload(grant):
    return {**asdict(grant), "categories": sorted(v.value for v in grant.categories),
            "planes": sorted(v.value for v in grant.planes), "rights_status": grant.rights_status.value}


def _restore_grant(raw):
    row = decode(raw, MAX_BATCH_BYTES)
    return SourceGrant(**{**row, "categories": frozenset(KnowledgeCategory(v) for v in row["categories"]),
        "planes": frozenset(AccessPlane(v) for v in row["planes"]),
        "rights_status": RightsStatus(row["rights_status"])})


def _prepare(raw, grants, now):
    data = decode(raw, MAX_BATCH_BYTES)
    _require(type(data) is dict and set(data) == {"schema", "source_id", "batch_id", "records"}
             and data["schema"] == BATCH_SCHEMA, "batch_schema")
    _id(data["source_id"]); _id(data["batch_id"])
    _require(type(grants) in (tuple, list) and len(grants) == 1
             and type(grants[0]) is SourceGrant, "single_typed_grant")
    grant = replace(grants[0])
    _require(grant.source_id == data["source_id"] and grant.point_in_time is True
             and grant.rights_status in (RightsStatus.INTERNAL, RightsStatus.VERIFIED)
             and AccessPlane.TRAINING in grant.planes, "source_not_permitted")
    _require(len(canonical(_grant_payload(grant))) <= MAX_GRANT_BYTES, "grant_payload_bound")
    records = data["records"]
    _require(type(records) is list and 1 <= len(records) <= MAX_BATCH_ROWS, "batch_row_bound")
    normalized, seen = [], set()
    for record in records:
        _require(type(record) is dict and set(record) == RECORD_FIELDS, "record_schema")
        for key in ("record_id", "subject", "feature", "schema_id"):
            _id(record[key])
        _require(record["record_id"] not in seen, "duplicate_source_record")
        seen.add(record["record_id"])
        _require(KnowledgeCategory(record["category"]) in grant.categories, "category_not_permitted")
        observed = _time(record["observed_at"])
        _require(observed <= now, "future_observation")
        normalized.append({**record, "value": str(_number(record["value"])),
                           "observed_at": observed.isoformat()})
    return data, grant, normalized


def _observation_id(tenant, source, record):
    return _sha(canonical([STORE_SCHEMA, tenant, source, record["record_id"]]))


def _observation(tenant, source, record, available):
    return FeatureObservation(_observation_id(tenant, source, record), record["subject"],
        record["feature"], _number(record["value"]), _time(record["observed_at"]),
        available, source, record["schema_id"])


def _receipt_digest(tenant, source, batch_id, raw, grant_raw, moment):
    return _sha(canonical([STORE_SCHEMA, tenant, source, batch_id, raw, grant_raw, moment]))


def _meta(db, tenant):
    rows = db.execute("SELECT schema,tenant_id FROM received_feature_meta").fetchall()
    _require(len(rows) == 1 and tuple(rows[0]) == (STORE_SCHEMA, tenant), "tenant_binding")


def validate_received_features(db, *, tenant_id):
    """Verify retained receipts against feature rows. Not external source authentication."""
    _require(db.in_transaction, "read_transaction_required")
    _meta(db, tenant_id)
    retained_bytes = db.execute("SELECT COALESCE(SUM(length(CAST(payload AS BLOB)) + length(CAST(grant_payload AS BLOB))),0) FROM received_feature_batches").fetchone()[0]
    _require(retained_bytes <= MAX_RECEIPT_BYTES, "receipt_byte_bound")
    receipts = db.execute("SELECT * FROM received_feature_batches ORDER BY sequence LIMIT ?",
                          (MAX_RECEIPTS + 1,)).fetchall()
    _require(len(receipts) <= MAX_RECEIPTS, "receipt_bound")
    features = db.execute("SELECT * FROM feature_observations LIMIT ?", (MAX_STORE_ROWS + 1,)).fetchall()
    _require(len(features) <= MAX_STORE_ROWS, "store_row_bound")
    expected, previous = {}, None
    for row in receipts:
        moment = _time(row["received_at"])
        _require(previous is None or moment >= previous, "receipt_time_order")
        previous = moment
        _require(row["sha256"] == _receipt_digest(tenant_id, row["source_id"], row["batch_id"],
            row["payload"], row["grant_payload"], row["received_at"]), "receipt_digest")
        data, _, records = _prepare(row["payload"].encode(),
            (_restore_grant(row["grant_payload"].encode()),), moment)
        _require(data["source_id"] == row["source_id"] and data["batch_id"] == row["batch_id"],
                 "receipt_identity")
        for record in records:
            item = _observation(tenant_id, row["source_id"], record, moment)
            record_raw = canonical([row["source_id"], record]).decode()
            old = expected.get(item.observation_id)
            _require(old is None or old[1] == record_raw, "source_record_changed")
            if old is None:
                expected[item.observation_id] = (item.payload(), record_raw)
    retained = db.execute("SELECT * FROM received_feature_records LIMIT ?", (MAX_STORE_ROWS + 1,)).fetchall()
    _require(len(features) == len(expected) == len(retained), "source_coverage")
    by_id = {row["observation_id"]: row for row in retained}
    for row in features:
        PointInTimeFeatureStore._verify_row(row)
        item = expected.get(row["observation_id"])
        saved = by_id.get(row["observation_id"])
        _require(item is not None and saved is not None, "record_lineage_missing")
        actual = FeatureObservation(row["observation_id"], row["subject"], row["feature"],
            _number(row["value"]), _time(row["observed_at"]), _time(row["available_at"]),
            row["source_id"], row["schema_id"]).payload()
        _require(actual == item[0] and saved["payload"] == item[1]
                 and saved["sha256"] == _sha(item[1].encode()), "record_lineage_mismatch")
    return {"observations": len(features), "receipts": len(receipts), "last_received_at": previous, "retained_bytes": retained_bytes}


class ReceivedFeatureStore(PointInTimeFeatureStore):
    """Dedicated local tenant scope. Never adopt a pre-existing unscoped feature store."""

    def __init__(self, path, *, tenant_id):
        self._lock = RLock()
        self.tenant_id = _id(tenant_id)
        path = Path(path).absolute()
        parent = path.parent.lstat()
        _require(stat.S_ISDIR(parent.st_mode) and parent.st_uid == os.geteuid()
                 and not stat.S_IMODE(parent.st_mode) & 0o077, "private_directory")
        new = not (path.exists() or path.is_symlink())
        if new:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        self.identity = _private(path)
        if not new:
            db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
            try:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA query_only=ON")
                db.execute("BEGIN")
                validate_received_features(db, tenant_id=tenant_id)
            finally:
                db.rollback()
                db.close()
        try:
            super().__init__(path)
            with self.db:
                self.db.execute("CREATE TABLE IF NOT EXISTS received_feature_meta (id INTEGER PRIMARY KEY CHECK(id=1), schema TEXT NOT NULL, tenant_id TEXT NOT NULL)")
                self.db.execute("CREATE TABLE IF NOT EXISTS received_feature_batches (sequence INTEGER PRIMARY KEY, source_id TEXT NOT NULL, batch_id TEXT NOT NULL, payload TEXT NOT NULL, grant_payload TEXT NOT NULL, received_at TEXT NOT NULL, sha256 TEXT NOT NULL, UNIQUE(source_id,batch_id))")
                self.db.execute("CREATE TABLE IF NOT EXISTS received_feature_records (observation_id TEXT PRIMARY KEY, payload TEXT NOT NULL, sha256 TEXT NOT NULL)")
                if new:
                    self.db.execute("INSERT INTO received_feature_meta VALUES(1,?,?)", (STORE_SCHEMA, tenant_id))
                for table in ("received_feature_meta", "received_feature_batches", "received_feature_records"):
                    for verb in ("UPDATE", "DELETE"):
                        self.db.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_{verb.lower()}_blocked BEFORE {verb} ON {table} BEGIN SELECT RAISE(ABORT,'Received features are append-only'); END")
                _meta(self.db, tenant_id)
                self.check_identity()
        except BaseException:
            if hasattr(self, "db"):
                self.db.close()
            raise

    def check_identity(self):
        _require(_private(self.path) == self.identity, "selected_store_replaced")

    def append(self, observation):
        raise ValueError("feature_ingestion_receipt_required")

    def snapshot(self, *, subject, as_of, requirements):
        with self._lock, self.db:
            self.db.execute("BEGIN")
            self.check_identity()
            validate_received_features(self.db, tenant_id=self.tenant_id)
            result = super().snapshot(subject=subject, as_of=as_of, requirements=requirements)
            self.check_identity()
        return result

    def receive(self, raw, *, source_grants, clock=None):
        _require(os.environ.get("TRADING_LIVE_MONEY_ACTIVE", "false") == "false", "paper_only")
        _require(type(raw) is bytes and type(source_grants) in (tuple, list)
                 and all(type(g) is SourceGrant for g in source_grants), "frozen_inputs")
        grants = tuple(replace(g) for g in source_grants)
        # Only this clock creates first-known availability. There is no input field for it.
        now = _time((clock or (lambda: datetime.now(timezone.utc)))())
        data, grant, records = _prepare(raw, grants, now)
        source, batch_id = data["source_id"], data["batch_id"]
        grant_raw, raw_text = canonical(_grant_payload(grant)).decode(), raw.decode()
        with self._lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.check_identity()
            state = validate_received_features(self.db, tenant_id=self.tenant_id)
            _require(state["last_received_at"] is None or now >= state["last_received_at"], "clock_regressed")
            old = self.db.execute("SELECT * FROM received_feature_batches WHERE source_id=? AND batch_id=?",
                                  (source, batch_id)).fetchone()
            if old is not None:
                _require(old["payload"] == raw_text and old["grant_payload"] == grant_raw,
                         "batch_identity_reused")
                self.check_identity()
                return {"mode": "OBSERVATION_ONLY", "received_at": old["received_at"],
                        "new_observations": 0, "duplicate_batch": True, "trading_authorized": False}
            _require(state["receipts"] < MAX_RECEIPTS, "new_receipt_bound")
            _require(state["retained_bytes"] + len(raw) + len(grant_raw.encode()) <= MAX_RECEIPT_BYTES, "new_receipt_byte_bound")
            added = 0
            for record in records:
                item = _observation(self.tenant_id, source, record, now)
                record_raw = canonical([source, record]).decode()
                prior = self.db.execute("SELECT payload FROM received_feature_records WHERE observation_id=?",
                                        (item.observation_id,)).fetchone()
                _require(prior is None or prior[0] == record_raw, "new_source_record_changed")
                if prior is None:
                    self._insert_observation(item)
                    self.db.execute("INSERT INTO received_feature_records VALUES(?,?,?)",
                                    (item.observation_id, record_raw, _sha(record_raw.encode())))
                    added += 1
            _require(state["observations"] + added <= MAX_STORE_ROWS, "new_store_row_bound")
            stamp = now.isoformat()
            digest = _receipt_digest(self.tenant_id, source, batch_id, raw_text, grant_raw, stamp)
            self.db.execute("INSERT INTO received_feature_batches(source_id,batch_id,payload,grant_payload,received_at,sha256) VALUES(?,?,?,?,?,?)",
                            (source, batch_id, raw_text, grant_raw, stamp, digest))
            validate_received_features(self.db, tenant_id=self.tenant_id)
            self.check_identity()
        return {"mode": "OBSERVATION_ONLY", "received_at": stamp, "new_observations": added,
                "duplicate_batch": False, "trading_authorized": False}
