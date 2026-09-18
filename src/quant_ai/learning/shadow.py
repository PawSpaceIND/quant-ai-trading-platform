"""Artifact-bound SHADOW forecasts. No broker, promotion, or executable model loader.

The caller supplies an already trained, versioned numeric logistic artifact. This
module verifies declared lineage, computes its probability, and records it in the
existing forecast journal. It does not prove dataset authenticity or model skill.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from pathlib import Path
from threading import RLock

from quant_ai.learning.contracts import TrainingDatasetManifest, TrainingRunManifest
from quant_ai.learning.outcomes import ForecastOutcomeJournal, ProbabilityForecast
from quant_ai.learning.training import training_dataset_digest

SCHEMA = "pramana.shadow_forecast.v1"
MODEL_SCHEMA = "pramana.shadow_logistic.v1"
INPUT_SCHEMA = "pramana.numeric_features.v1"
EVENT = "positive_long_return_after_cost"
MAX_PAYLOAD_BYTES = 65536
MAX_FORECASTS = 10000
TABLES = {"shadow_journal_meta", "shadow_model_bindings", "shadow_forecast_inputs"}
ID = re.compile(r"[A-Za-z0-9._:/-]{1,180}")
SHA = re.compile(r"[0-9a-f]{64}")
MODEL_FIELDS = {"schema", "feature_names", "coefficients", "intercept", "horizon_seconds",
                "maximum_feature_age_seconds", "cost_policy_id", "cost_policy_sha256", "event"}
INPUT_FIELDS = {"schema", "subject", "observed_at", "available_at", "source_ids", "values"}


def _check(condition, code):
    if not condition:
        raise ValueError("shadow_" + code)


def _instant(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    _check(isinstance(value, datetime) and value.tzinfo is not None
           and value.utcoffset() is not None, "aware_time_required")
    return value.astimezone(timezone.utc)


def _identifier(value):
    _check(isinstance(value, str) and ID.fullmatch(value), "identity_invalid")
    return value


def _decimal(value):
    _check(isinstance(value, str) and 0 < len(value) <= 128 and value == value.strip(),
           "numeric_string_required")
    number = Decimal(value)
    _check(number.is_finite() and number.copy_abs() <= Decimal("1e12"), "numeric_bound")
    return number


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _hash(value):
    return _sha(_canonical(value).encode())


def _unique(pairs):
    result = {}
    for key, value in pairs:
        _check(key not in result, "duplicate_json_key")
        result[key] = value
    return result


def decode(raw):
    _check(isinstance(raw, bytes) and 0 < len(raw) <= MAX_PAYLOAD_BYTES, "payload_size")
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("shadow_nonfinite_json")))


def feature_schema_digest(names):
    return _hash({"schema": INPUT_SCHEMA, "names": list(names)})


def label_schema_digest(model):
    return _hash({"event": EVENT, "horizon_seconds": model["horizon_seconds"],
                  "cost_policy_sha256": model["cost_policy_sha256"]})


def _manifest_payload(manifest):
    result = asdict(manifest)
    for key, value in result.items():
        if isinstance(value, datetime):
            result[key] = _instant(value).isoformat()
    return result


@dataclass(frozen=True)
class ShadowModelBundle:
    dataset: TrainingDatasetManifest
    training_run: TrainingRunManifest
    artifact: bytes

    def __post_init__(self):
        _check(isinstance(self.dataset, TrainingDatasetManifest)
               and isinstance(self.training_run, TrainingRunManifest), "typed_manifests_required")
        object.__setattr__(self, "dataset", replace(self.dataset))
        object.__setattr__(self, "training_run", replace(self.training_run))
        self.validate()

    def validate(self):
        data, run = replace(self.dataset), replace(self.training_run)
        _identifier(run.candidate_id)
        _check(run.dataset_id == data.dataset_id
               and run.dataset_manifest_sha256 == training_dataset_digest(data), "dataset_binding")
        _check(isinstance(self.artifact, bytes) and len(self.artifact) <= MAX_PAYLOAD_BYTES and _sha(self.artifact) == run.artifact_sha256,
               "artifact_binding")
        _check(run.trained_at >= data.cutoff and run.model_family == "decimal_logistic", "training_contract")
        model = decode(self.artifact)
        _check(isinstance(model, dict) and set(model) == MODEL_FIELDS
               and model["schema"] == MODEL_SCHEMA and model["event"] == EVENT, "model_schema")
        names, weights = model["feature_names"], model["coefficients"]
        _check(isinstance(names, list) and 1 <= len(names) <= 64
               and all(isinstance(n, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", n) for n in names)
               and len(set(names)) == len(names), "feature_names")
        _check(isinstance(weights, list) and len(weights) == len(names), "coefficient_shape")
        for value in [*weights, model["intercept"]]:
            _decimal(value)
        for field, ceiling in (("horizon_seconds", 86400), ("maximum_feature_age_seconds", 86400)):
            _check(type(model[field]) is int and 1 <= model[field] <= ceiling, "time_budget")
        _check(isinstance(model["cost_policy_sha256"], str)
               and SHA.fullmatch(model["cost_policy_sha256"]), "cost_digest")
        _check(model["cost_policy_id"] == data.cost_policy_id, "cost_identity")
        _check(data.feature_schema_sha256 == feature_schema_digest(names), "feature_schema_binding")
        _check(data.label_schema_sha256 == label_schema_digest(model), "label_schema_binding")
        return model

    def payload(self):
        self.validate()
        return {"schema": SCHEMA, "dataset": _manifest_payload(self.dataset),
                "training_run": _manifest_payload(self.training_run),
                "artifact_text": self.artifact.decode("utf-8")}

    @classmethod
    def from_payload(cls, payload):
        _check(isinstance(payload, dict) and set(payload) == {
            "schema", "dataset", "training_run", "artifact_text"} and payload["schema"] == SCHEMA,
            "bundle_schema")
        dataset, run = dict(payload["dataset"]), dict(payload["training_run"])
        dataset["cutoff"] = _instant(dataset["cutoff"])
        run["trained_at"] = _instant(run["trained_at"])
        return cls(TrainingDatasetManifest(**dataset), TrainingRunManifest(**run),
                   payload["artifact_text"].encode("utf-8"))


def _input(snapshot, bundle, now):
    model = bundle.validate()
    # Canonical copy prevents caller mutation changing the vector after validation.
    value = decode(_canonical(snapshot).encode())
    _check(isinstance(value, dict) and set(value) == INPUT_FIELDS
           and value["schema"] == INPUT_SCHEMA, "input_schema")
    _identifier(value["subject"])
    observed, available = _instant(value["observed_at"]), _instant(value["available_at"])
    _check(observed <= available <= now and now - observed <= timedelta(
        seconds=model["maximum_feature_age_seconds"]), "input_time")
    _check(bundle.dataset.cutoff < now and bundle.training_run.trained_at <= now, "training_time")
    sources = value["source_ids"]
    _check(isinstance(sources, list) and 1 <= len(sources) <= 64
           and len(set(sources)) == len(sources), "input_sources")
    for source in sources:
        _identifier(source)
    _check(isinstance(value["values"], dict)
           and set(value["values"]) == set(model["feature_names"]), "input_feature_schema")
    for number in value["values"].values():
        _decimal(number)
    value["observed_at"], value["available_at"] = observed.isoformat(), available.isoformat()
    return value, model


def predict(bundle, snapshot, *, now):
    """Execute only the declared bounded numeric model; this never trains or promotes."""
    _check(type(bundle) is ShadowModelBundle, "typed_bundle_required")
    now = _instant(now)
    value, model = _input(snapshot, bundle, now)
    with localcontext() as context:
        context.prec = 34
        logit = _decimal(model["intercept"]) + sum((
            _decimal(weight) * _decimal(value["values"][name])
            for name, weight in zip(model["feature_names"], model["coefficients"])
        ), Decimal(0))
        _check(logit.is_finite() and abs(logit) <= 60, "logit_outside_supported_domain")
        probability = Decimal(1) / (Decimal(1) + (-logit).exp())
    return probability, value, model


def _private_existing(path):
    info = path.lstat()
    _check(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
           and info.st_uid == os.geteuid() and not stat.S_IMODE(info.st_mode) & 0o077,
           "private_unaliased_file_required")
    return info.st_dev, info.st_ino


def _meta(db, tenant):
    rows = db.execute("SELECT schema,tenant_id FROM shadow_journal_meta").fetchall()
    _check(len(rows) == 1 and tuple(rows[0]) == (SCHEMA, tenant), "journal_tenant_binding")


class ShadowForecastWriter:
    """Explicit local shadow writer. No daemon hook, broker handle or promotion API.

    Existing unscoped journals cannot be adopted. Each method serializes the
    existing forecast insert and its source records in one SQLite transaction.
    """

    def __init__(self, path, *, tenant_id, clock=None):
        self.tenant_id = _identifier(tenant_id)
        self.path = Path(path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.exists() or self.path.is_symlink():
            _private_existing(self.path)
            with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                _meta(db, tenant_id)  # Before any constructor can create schema in an existing file.
            new = False
        else:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            new = True
        self._file_identity = _private_existing(self.path)
        self.journal = ForecastOutcomeJournal(self.path)
        try:
            with self.journal.db as db:
                db.execute("CREATE TABLE IF NOT EXISTS shadow_journal_meta (id INTEGER PRIMARY KEY CHECK(id=1), schema TEXT NOT NULL, tenant_id TEXT NOT NULL)")
                db.execute("CREATE TABLE IF NOT EXISTS shadow_model_bindings (candidate_id TEXT PRIMARY KEY, payload TEXT NOT NULL, sha256 TEXT NOT NULL)")
                db.execute("CREATE TABLE IF NOT EXISTS shadow_forecast_inputs (forecast_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, payload TEXT NOT NULL, sha256 TEXT NOT NULL, recorded_at TEXT NOT NULL)")
                if new:
                    db.execute("INSERT INTO shadow_journal_meta VALUES(1,?,?)", (SCHEMA, tenant_id))
                for table in sorted(TABLES):
                    for verb in ("UPDATE", "DELETE"):
                        db.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_{verb.lower()}_blocked BEFORE {verb} ON {table} BEGIN SELECT RAISE(ABORT,'Shadow lineage is append-only'); END")
                _meta(db, tenant_id)
        except BaseException:
            self.journal.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.journal.close()

    def record(self, bundle, snapshot, *, pair_id):
        _identifier(pair_id)
        _check(type(bundle) is ShadowModelBundle, "typed_bundle_required")
        _check(_private_existing(self.path) == self._file_identity, "journal_path_replaced")
        model_payload = bundle.payload()
        raw_model = _canonical(model_payload)
        _check(len(raw_model.encode()) <= MAX_PAYLOAD_BYTES, "bundle_payload_size")
        bundle = ShadowModelBundle.from_payload(decode(raw_model.encode()))
        snapshot = decode(_canonical(snapshot).encode())
        candidate = bundle.training_run.candidate_id
        # Validate the input again at the original decision instant on exact retry.
        with self._lock, self.journal.db as db:
            db.execute("BEGIN IMMEDIATE")
            validate_shadow_lineage(db, tenant_id=self.tenant_id)
            old = db.execute("SELECT * FROM probability_forecasts WHERE pair_id=? AND candidate_id=?",
                             (pair_id, candidate)).fetchone()
            _check(old is not None or db.execute("SELECT COUNT(*) FROM probability_forecasts").fetchone()[0] < MAX_FORECASTS, "journal_row_limit")
            moment = _instant(old["decision_at"]) if old else _instant(self.clock())
            _check(_private_existing(self.path) == self._file_identity, "journal_path_replaced")
            probability, vector, model = predict(bundle, snapshot, now=moment)
            input_payload = {"schema": SCHEMA, "tenant_id": self.tenant_id,
                             "candidate_id": candidate, "pair_id": pair_id,
                             "decision_at": moment.isoformat(), "input": vector,
                             "model_binding_sha256": _hash(model_payload)}
            forecast_id = _hash([SCHEMA, self.tenant_id, candidate, pair_id])
            item = ProbabilityForecast(forecast_id, pair_id, candidate, vector["subject"],
                probability, moment, moment + timedelta(seconds=model["horizon_seconds"]),
                _hash(vector), bundle.training_run.artifact_sha256, model["cost_policy_sha256"])
            existing_model = db.execute("SELECT payload FROM shadow_model_bindings WHERE candidate_id=?", (candidate,)).fetchone()
            _check(existing_model is None or existing_model[0] == raw_model, "candidate_artifact_reused")
            db.execute("INSERT OR IGNORE INTO shadow_model_bindings VALUES(?,?,?)",
                       (candidate, raw_model, _hash(model_payload)))
            raw_input = _canonical(input_payload)
            _check(len(raw_input.encode()) <= MAX_PAYLOAD_BYTES, "input_payload_size")
            existing_input = db.execute("SELECT payload FROM shadow_forecast_inputs WHERE forecast_id=?", (forecast_id,)).fetchone()
            _check(existing_input is None or existing_input[0] == raw_input, "pair_input_reused")
            self.journal._insert_forecast(item)
            db.execute("INSERT OR IGNORE INTO shadow_forecast_inputs VALUES(?,?,?,?,?)",
                       (forecast_id, candidate, raw_input, _hash(input_payload), moment.isoformat()))
            return item


def validate_shadow_lineage(db, *, tenant_id):
    """Read-only local consistency check; hashes do not authenticate a model or provider."""
    _meta(db, tenant_id)
    rows = db.execute("SELECT f.*,i.payload AS input_payload,i.candidate_id AS input_candidate,i.sha256 AS input_sha,i.recorded_at,m.payload AS model_payload,m.sha256 AS model_sha FROM probability_forecasts f LEFT JOIN shadow_forecast_inputs i USING(forecast_id) LEFT JOIN shadow_model_bindings m ON m.candidate_id=f.candidate_id ORDER BY f.forecast_id LIMIT ?", (MAX_FORECASTS + 1,)).fetchall()
    _check(len(rows) <= MAX_FORECASTS, "journal_row_limit")
    _check(db.execute("SELECT COUNT(*) FROM shadow_forecast_inputs").fetchone()[0] == len(rows), "orphan_input_lineage")
    for row in rows:
        _check(isinstance(row["input_payload"], str) and isinstance(row["model_payload"], str), "forecast_lineage_missing")
        inputs, models = decode(row["input_payload"].encode()), decode(row["model_payload"].encode())
        _check(_hash(inputs) == row["input_sha"] and _hash(models) == row["model_sha"], "lineage_digest")
        _check(isinstance(inputs, dict) and set(inputs) == {"schema", "tenant_id", "candidate_id", "pair_id", "decision_at", "input", "model_binding_sha256"}, "input_lineage_schema")
        bundle = ShadowModelBundle.from_payload(models)
        _check(inputs["tenant_id"] == tenant_id and inputs["schema"] == SCHEMA
               and inputs["candidate_id"] == row["candidate_id"] == row["input_candidate"] == bundle.training_run.candidate_id
               and inputs["pair_id"] == row["pair_id"]
               and inputs["decision_at"] == row["decision_at"] == row["recorded_at"]
               and inputs["model_binding_sha256"] == row["model_sha"], "forecast_lineage_binding")
        moment = _instant(row["decision_at"])
        probability, vector, model = predict(bundle, inputs["input"], now=moment)
        expected = ProbabilityForecast(_hash([SCHEMA, tenant_id, row["candidate_id"], row["pair_id"]]),
            row["pair_id"], row["candidate_id"], vector["subject"], probability, moment,
            moment + timedelta(seconds=model["horizon_seconds"]), _hash(vector),
            bundle.training_run.artifact_sha256, model["cost_policy_sha256"])
        payload = ForecastOutcomeJournal._forecast_payload(expected)
        _check(_hash(payload) == row["payload_sha256"] and str(probability) == row["probability"]
               and row["forecast_id"] == expected.forecast_id
               and row["subject"] == expected.subject and row["feature_snapshot_sha256"] == expected.feature_snapshot_sha256
               and row["model_artifact_sha256"] == expected.model_artifact_sha256
               and row["cost_policy_sha256"] == expected.cost_policy_sha256
               and row["resolve_after"] == expected.resolve_after.isoformat()
               and row["regime"] == expected.regime, "forecast_replay_mismatch")
    return {"schema": SCHEMA, "tenant_id": tenant_id, "verified_forecasts": len(rows)}
