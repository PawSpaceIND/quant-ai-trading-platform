"""Read recorded point-in-time observations into a training-only data package.

The selected database and source grants are operator choices, not authenticated
provider or account identities. No feature, endpoint price or cost is fabricated.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from pathlib import Path

from quant_ai.features.store import (
    FeatureObservation,
    FeatureRequirement,
    PointInTimeFeatureStore,
)
from quant_ai.learning.contracts import AccessPlane, KnowledgeCategory, RightsStatus, SourceGrant

PLAN_SCHEMA = "pramana.feature_training_plan.v1"
PACKAGE_SCHEMA = "pramana.feature_training_package.v1"
DATA_SCHEMA = "pramana.logistic_training_data.v1"
MAX_PLAN_BYTES = 500_000
MAX_PACKAGE_BYTES = 12_000_000
MAX_SOURCE_ROWS = 100_000
MAX_DATA_BYTES = 2_000_000
ID = re.compile(r"[A-Za-z0-9._:/-]{1,180}")
SHA = re.compile(r"[0-9a-f]{64}")
PLAN_FIELDS = {"schema", "partition", "dataset_id", "cutoff", "horizon_seconds",
               "maximum_feature_age_seconds", "cost_policy_id", "cost_policy_sha256",
               "adjustment_policy_id", "features", "price", "cost", "decisions"}
RULE_FIELDS = {"feature", "source_id", "schema_id", "max_age_seconds", "category"}
PACKAGE_FIELDS = {"schema", "assembled_at", "plan", "data", "observations", "sha256"}


def _check(condition, reason):
    if not condition:
        raise ValueError("feature_dataset_" + reason)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _pairs(pairs):
    output = {}
    for key, value in pairs:
        _check(key not in output, "duplicate_json_key")
        output[key] = value
    return output


def decode(raw, maximum):
    _check(type(raw) is bytes and 0 < len(raw) <= maximum, "input_bound")
    return json.loads(raw, object_pairs_hook=_pairs, parse_constant=lambda _: (
        _check(False, "nonfinite_json")))


def _time(value):
    if type(value) is str:
        value = datetime.fromisoformat(value)
    _check(type(value) is datetime and value.tzinfo is not None
           and value.utcoffset() is not None, "aware_time")
    return value.astimezone(timezone.utc)


def _id(value):
    _check(type(value) is str and ID.fullmatch(value), "identity")
    return value


def _number(value):
    _check(type(value) is str and 0 < len(value) <= 128 and value == value.strip(), "numeric_text")
    number = Decimal(value)
    _check(number.is_finite() and number.copy_abs() <= Decimal("1e12")
           and -24 <= number.as_tuple().exponent <= 12, "numeric_bound")
    return number


def _private(path):
    info = path.lstat()
    _check(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
           and info.st_uid == os.geteuid() and not stat.S_IMODE(info.st_mode) & 0o077,
           "private_source")
    return info.st_dev, info.st_ino


class ReadOnlyFeatureSource(PointInTimeFeatureStore):
    """Reuse the existing selector without invoking its schema-writing constructor."""

    def __init__(self, path):
        self.path = Path(path).absolute()
        self.identity = _private(self.path)
        self.db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=10)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA query_only=ON")
            self.db.execute("SELECT observation_id FROM feature_observations LIMIT 0")
            self.check_identity()
        except BaseException:
            self.db.close()
            raise

    def check_identity(self):
        _check(_private(self.path) == self.identity, "source_replaced")

    def append(self, observation):
        raise ValueError("feature_dataset_read_only")


def _plan(raw, grants, now):
    plan = decode(raw, MAX_PLAN_BYTES)
    _check(type(plan) is dict and set(plan) == PLAN_FIELDS
           and plan["schema"] == PLAN_SCHEMA and plan["partition"] == "training", "plan_schema")
    for name in ("dataset_id", "cost_policy_id", "adjustment_policy_id"):
        _id(plan[name])
    _check(type(plan["cost_policy_sha256"]) is str and SHA.fullmatch(plan["cost_policy_sha256"]),
           "cost_policy")
    cutoff = _time(plan["cutoff"])
    _check(cutoff <= now, "future_cutoff")
    for key in ("horizon_seconds", "maximum_feature_age_seconds"):
        _check(type(plan[key]) is int and 1 <= plan[key] <= 86400, "time_bound")
    features = plan["features"]
    _check(type(features) is list and 1 <= len(features) <= 16, "feature_count")
    _check(type(grants) in (tuple, list) and 1 <= len(grants) <= 64
           and all(type(g) is SourceGrant for g in grants), "typed_grants")
    selected = {g.source_id: replace(g) for g in grants}
    _check(len(selected) == len(grants), "duplicate_grant")
    rules = [*features, plan["price"], plan["cost"]]
    for rule in rules:
        _check(type(rule) is dict and set(rule) == RULE_FIELDS, "rule_schema")
        for name in ("feature", "source_id", "schema_id"):
            _id(rule[name])
        _check(type(rule["max_age_seconds"]) is int
               and 1 <= rule["max_age_seconds"] <= plan["maximum_feature_age_seconds"], "rule_age")
        grant = selected.get(rule["source_id"])
        _check(grant is not None and AccessPlane.TRAINING in grant.planes
               and grant.point_in_time and grant.rights_status in (RightsStatus.INTERNAL, RightsStatus.VERIFIED)
               and KnowledgeCategory(rule["category"]) in grant.categories, "source_not_permitted")
    _check(set(selected) == {r["source_id"] for r in rules}, "grant_scope")
    names = [r["feature"] for r in features]
    _check(len(set(names)) == len(names) and all(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", n)
           for n in names), "feature_names")
    _check(plan["price"]["category"] == "MARKET" and plan["cost"]["category"] == "BROKER",
           "price_cost_categories")
    _check(plan["price"]["schema_id"] == "price:" + plan["adjustment_policy_id"]
           and plan["cost"]["schema_id"] == "cost_fraction:" + plan["cost_policy_sha256"],
           "price_cost_binding")
    _check(plan["price"]["feature"] != plan["cost"]["feature"]
           and not set(names) & {plan["price"]["feature"], plan["cost"]["feature"]}, "role_collision")
    decisions = plan["decisions"]
    _check(type(decisions) is list and 1 <= len(decisions) <= 2000, "decision_bound")
    seen, windows, previous = set(), {}, None
    for decision in decisions:
        _check(type(decision) is dict and set(decision) == {"row_id", "subject", "decision_at"},
               "decision_schema")
        row_id, subject = _id(decision["row_id"]), _id(decision["subject"])
        at = _time(decision["decision_at"])
        _check(previous is None or at >= previous, "decision_order")
        _check(row_id not in seen and (subject not in windows or at >= windows[subject]),
               "duplicate_or_overlap")
        due = at + timedelta(seconds=plan["horizon_seconds"])
        _check(due <= cutoff, "outcome_not_due")
        seen.add(row_id)
        previous, windows[subject] = at, due
    return plan, selected


def _retain(point, subject, as_of, rule, grants, observations, *, endpoint=False):
    item = FeatureObservation(point.observation_id, subject, point.feature, point.value,
                              point.observed_at, point.available_at, point.source_id, point.schema_id)
    _number(str(item.value))
    observed, available = _time(item.observed_at), _time(item.available_at)
    _check(observed <= available <= as_of and item.source_id == rule["source_id"]
           and item.schema_id == rule["schema_id"] and item.feature == rule["feature"], "point_binding")
    if not endpoint:
        age = (as_of - observed).total_seconds()
        limit = grants[item.source_id].max_age_seconds
        _check(age <= rule["max_age_seconds"] and (limit is None or age <= limit), "point_age")
    payload = item.payload()
    old = observations.get(item.observation_id)
    _check(old is None or old == payload, "observation_reuse")
    observations[item.observation_id] = payload
    return item


def _assemble(store, plan, grants):
    # The caller holds one read transaction for this whole extraction.
    _check(store.db.execute("SELECT COUNT(*) FROM feature_observations").fetchone()[0]
           <= MAX_SOURCE_ROWS, "source_row_bound")
    rules = [*plan["features"], plan["price"], plan["cost"]]
    requirements = tuple(FeatureRequirement(r["feature"], r["max_age_seconds"],
                         frozenset({r["source_id"]}), r["schema_id"]) for r in rules)
    by_name = {r["feature"]: r for r in rules}
    cutoff = _time(plan["cutoff"])
    rows, observations = [], {}
    for decision in plan["decisions"]:
        subject, at = decision["subject"], _time(decision["decision_at"])
        due = at + timedelta(seconds=plan["horizon_seconds"])
        snapshot = store.snapshot(subject=subject, as_of=at, requirements=requirements)
        points = {p.feature: _retain(p, subject, at, by_name[p.feature], grants, observations)
                  for p in snapshot.points}
        price, cost = points[plan["price"]["feature"]], points[plan["cost"]["feature"]]
        rule = plan["price"]
        # Require that precise endpoint. A later quote or another adjustment basis is not equivalent.
        row = store.db.execute("""SELECT * FROM feature_observations WHERE subject=? AND feature=?
            AND source_id=? AND schema_id=? AND observed_at=? AND available_at<=?
            ORDER BY available_at,observation_id LIMIT 1""", (subject, rule["feature"], rule["source_id"],
            rule["schema_id"], due.isoformat(), cutoff.isoformat())).fetchone()
        _check(row is not None, "exact_endpoint_missing")
        store._verify_row(row)
        endpoint = _retain(store._point(row), subject, cutoff, rule, grants, observations, endpoint=True)
        _check(_time(endpoint.observed_at) == due, "endpoint_time")
        _check(price.value > 0 and endpoint.value > 0 and 0 <= cost.value <= 1, "price_cost_value")
        with localcontext(Context(prec=96, rounding=ROUND_HALF_EVEN)):
            delta = endpoint.value - price.value
            gross = (delta / price.value).quantize(Decimal("1e-24"))
            _check((delta - price.value * cost.value > 0) == (gross - cost.value > 0),
                   "rounding_changes_label")
            _check(-1 <= gross <= 10 and gross - cost.value >= -1, "return_bound")
        row_sources = sorted({p.source_id for p in points.values()} | {endpoint.source_id})
        oldest = min(_time(p.observed_at) for p in points.values())
        _check(all(grants[source].max_age_seconds is None
                   or (at - oldest).total_seconds() <= grants[source].max_age_seconds
                   for source in row_sources), "aggregate_feature_age")
        rows.append({**decision, "decision_at": at.isoformat(),
            "observed_at": min(_time(p.observed_at) for p in points.values()).isoformat(),
            "available_at": max(_time(p.available_at) for p in points.values()).isoformat(),
            "resolve_after": due.isoformat(), "outcome_available_at": _time(endpoint.available_at).isoformat(),
            "source_ids": row_sources, "values": {r["feature"]: str(points[r["feature"]].value)
                for r in plan["features"]}, "gross_return": str(gross), "cost_fraction": str(cost.value)})
    data = {k: plan[k] for k in ("partition", "dataset_id", "horizon_seconds", "maximum_feature_age_seconds",
                               "cost_policy_id", "cost_policy_sha256", "adjustment_policy_id")}
    data.update(schema=DATA_SCHEMA, cutoff=cutoff.isoformat(), source_ids=sorted(grants),
                feature_names=[r["feature"] for r in plan["features"]], rows=rows)
    _check(len(canonical(data)) <= MAX_DATA_BYTES, "data_size")
    return data, [observations[k] for k in sorted(observations)]


def build_training_package(source, plan_raw, *, source_grants, now):
    """All requested rows or refusal. Never selects rows based on observed outcomes."""
    moment = _time(now)
    plan, grants = _plan(plan_raw, source_grants, moment)
    with ReadOnlyFeatureSource(source) as store:
        store.db.execute("BEGIN")
        try:
            data, observations = _assemble(store, plan, grants)
            store.check_identity()
        finally:
            store.db.rollback()
    payload = {"schema": PACKAGE_SCHEMA, "assembled_at": moment.isoformat(), "plan": plan,
               "data": data, "observations": observations}
    payload["sha256"] = _digest(canonical(payload))
    raw = canonical(payload)
    _check(len(raw) <= MAX_PACKAGE_BYTES, "package_size")
    return raw


def validate_training_package(raw, *, source_grants, now):
    """Replay selected records; not proof of source completeness or provider authenticity."""
    payload = decode(raw, MAX_PACKAGE_BYTES)
    _check(type(payload) is dict and set(payload) == PACKAGE_FIELDS
           and payload["schema"] == PACKAGE_SCHEMA, "package_schema")
    _check(payload["sha256"] == _digest(canonical({k: v for k, v in payload.items() if k != "sha256"})),
           "package_digest")
    assembled, current = _time(payload["assembled_at"]), _time(now)
    _check(assembled <= current, "future_package")
    plan, grants = _plan(canonical(payload["plan"]), source_grants, assembled)
    records = payload["observations"]
    _check(type(records) is list and 1 <= len(records) <= 38_000, "lineage_bound")
    with PointInTimeFeatureStore(":memory:") as store:
        seen = set()
        for row in records:
            _check(type(row) is dict and set(row) == {"observationId", "subject", "feature", "value",
                   "observedAt", "availableAt", "sourceId", "schemaId"}, "lineage_schema")
            _check(row["observationId"] not in seen, "duplicate_lineage")
            seen.add(row["observationId"])
            store.append(FeatureObservation(row["observationId"], row["subject"], row["feature"],
                _number(row["value"]), _time(row["observedAt"]), _time(row["availableAt"]),
                row["sourceId"], row["schemaId"]))
        data, observations = _assemble(store, plan, grants)
    _check(data == payload["data"] and observations == records, "lineage_replay")
    return canonical(data)


def fit_training_package(raw, *, source_grants, run_id, candidate_id, clock=None):
    """Use the unchanged fitter, binding its configuration to the reviewed data package."""
    from quant_ai.learning.fitting import FittedCandidate, fit_candidate
    from quant_ai.learning.shadow import ShadowModelBundle

    _check(type(source_grants) in (tuple, list) and all(type(g) is SourceGrant for g in source_grants),
           "fit_grants")
    source_grants = tuple(replace(g) for g in source_grants)
    clock = clock or (lambda: datetime.now(timezone.utc))
    data = validate_training_package(raw, source_grants=source_grants, now=clock())
    result = fit_candidate(data, source_grants=source_grants, run_id=run_id,
                           candidate_id=candidate_id, clock=clock)
    binding = {"schema": "pramana.feature_training_binding.v1", "package_sha256": _digest(raw),
               "fitter_configuration_sha256": result.bundle.training_run.configuration_sha256}
    run = replace(result.bundle.training_run, configuration_sha256=_digest(canonical(binding)))
    bundle = ShadowModelBundle(result.bundle.dataset, run, result.bundle.artifact)
    diagnostics = result.diagnostics
    diagnostics["data_package_binding"] = binding
    return FittedCandidate(bundle, canonical(diagnostics).decode())
