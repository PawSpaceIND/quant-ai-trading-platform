"""Offline training-only logistic fitting; no provider, broker or promotion calls.

Source grants and timestamps are caller declarations, not independent attestation.
The output is a candidate for the existing shadow writer, not calibrated skill.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from pathlib import Path

from quant_ai.learning.contracts import (
    AccessPlane,
    RightsStatus,
    SourceGrant,
    TrainingDatasetManifest,
)
from quant_ai.learning.shadow import (
    EVENT,
    MODEL_SCHEMA,
    SHA,
    ShadowModelBundle,
    _canonical,
    _decimal,
    _identifier,
    _instant,
    _unique,
    feature_schema_digest,
    label_schema_digest,
)
from quant_ai.learning.training import execute_training

SCHEMA = "pramana.logistic_training_data.v1"
FIT_SCHEMA = "pramana.logistic_fit.v1"
MAX_INPUT_BYTES = 2_000_000
MIN_ROWS = 30
MIN_CLASS_ROWS = 5
MAX_ROWS = 2000
MAX_FEATURES = 16
MAX_WORK = 20_000_000
FIELDS = {"schema", "partition", "dataset_id", "cutoff", "source_ids", "feature_names",
          "horizon_seconds", "maximum_feature_age_seconds", "cost_policy_id",
          "cost_policy_sha256", "adjustment_policy_id", "rows"}
ROW_FIELDS = {"row_id", "subject", "decision_at", "observed_at", "available_at",
              "resolve_after", "outcome_available_at", "source_ids", "values",
              "gross_return", "cost_fraction"}


def _require(condition, reason):
    if not condition:
        raise ValueError("logistic_fit_" + reason)


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _number(value):
    result = _decimal(value)
    _require(-24 <= result.as_tuple().exponent <= 12, "numeric_precision_bound")
    return result


def _sources(values):
    _require(type(values) is list and 1 <= len(values) <= 64, "sources_required")
    for value in values:
        _identifier(value)
    _require(len(set(values)) == len(values), "duplicate_source")
    return tuple(values)


@dataclass(frozen=True)
class FitConfig:
    l2: Decimal = Decimal("0.1")
    gradient_tolerance: Decimal = Decimal("0.000001")
    maximum_iterations: int = 512

    def __post_init__(self):
        _require(type(self.l2) is Decimal and self.l2.is_finite()
                 and Decimal("0.01") <= self.l2 <= 1, "regularization_bound")
        _require(type(self.gradient_tolerance) is Decimal
                 and self.gradient_tolerance.is_finite()
                 and Decimal("1e-10") <= self.gradient_tolerance <= Decimal("0.0001"),
                 "tolerance_bound")
        _require(type(self.maximum_iterations) is int
                 and 1 <= self.maximum_iterations <= 2000, "iteration_bound")

    def payload(self):
        return {"algorithm": "full_batch_scaled_logistic_v1", "l2": str(self.l2),
                "gradient_tolerance": str(self.gradient_tolerance),
                "maximum_iterations": self.maximum_iterations,
                "precision": 34, "initialization": "zeros", "seed": 0,
                "scaling": "training_only_max_abs_at_least_one"}


@dataclass(frozen=True)
class FittedCandidate:
    bundle: ShadowModelBundle
    diagnostics_json: str

    @property
    def diagnostics(self):
        return json.loads(self.diagnostics_json)


def _read_dataset(raw, grants, config, now):
    _require(type(raw) is bytes and 0 < len(raw) <= MAX_INPUT_BYTES, "input_size")
    data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(
                          ValueError("logistic_fit_nonfinite_json")))
    _require(type(data) is dict and set(data) == FIELDS and data["schema"] == SCHEMA,
             "dataset_schema")
    _require(data["partition"] == "training", "training_partition_required")
    for key in ("dataset_id", "cost_policy_id", "adjustment_policy_id"):
        _identifier(data[key])
    _require(type(data["cost_policy_sha256"]) is str
             and SHA.fullmatch(data["cost_policy_sha256"]), "cost_digest")
    cutoff = _instant(data["cutoff"])
    _require(cutoff <= now, "future_dataset")
    sources = _sources(data["source_ids"])
    _require(type(grants) in (tuple, list) and 1 <= len(grants) <= 64
             and all(type(g) is SourceGrant for g in grants),
             "typed_source_grants_required")
    grants = tuple(replace(g) for g in grants)
    _require(len({g.source_id for g in grants}) == len(grants), "duplicate_grant")
    selected = {g.source_id: g for g in grants}
    _require(set(selected) == set(sources), "source_grant_scope")
    for grant in grants:
        _require(AccessPlane.TRAINING in grant.planes and grant.point_in_time
                 and grant.rights_status in (RightsStatus.INTERNAL, RightsStatus.VERIFIED),
                 "training_source_not_permitted")
    names = data["feature_names"]
    _require(type(names) is list and 1 <= len(names) <= MAX_FEATURES
             and all(type(n) is str for n in names) and len(set(names)) == len(names),
             "feature_schema")
    _require(all(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", n) for n in names),
             "feature_schema")
    for key in ("horizon_seconds", "maximum_feature_age_seconds"):
        _require(type(data[key]) is int and 1 <= data[key] <= 86400, "time_budget")
    records = data["rows"]
    _require(type(records) is list and MIN_ROWS <= len(records) <= MAX_ROWS, "row_count")
    _require(len(records) * len(names) * config.maximum_iterations <= MAX_WORK, "work_budget")
    rows, labels, seen, pairs, windows, used_sources = [], [], set(), set(), {}, set()
    previous = None
    for row in records:
        _require(type(row) is dict and set(row) == ROW_FIELDS, "row_schema")
        row_id, subject = _identifier(row["row_id"]), _identifier(row["subject"])
        moment = _instant(row["decision_at"])
        observed, available = _instant(row["observed_at"]), _instant(row["available_at"])
        resolved, known = _instant(row["resolve_after"]), _instant(row["outcome_available_at"])
        _require(observed <= available <= moment and moment - observed <= timedelta(
            seconds=data["maximum_feature_age_seconds"]), "future_or_stale_feature")
        _require(resolved == moment + timedelta(seconds=data["horizon_seconds"])
                 and resolved <= known <= cutoff, "outcome_not_known_at_cutoff")
        _require(previous is None or moment >= previous, "nonchronological_rows")
        _require(row_id not in seen and (subject, moment) not in pairs, "duplicate_row")
        _require(subject not in windows or moment >= windows[subject], "overlapping_outcomes")
        row_sources = _sources(row["source_ids"])
        _require(set(row_sources) <= set(sources), "unknown_row_source")
        for source in row_sources:
            age_limit = selected[source].max_age_seconds
            _require(age_limit is None or (moment - observed).total_seconds() <= age_limit,
                     "source_age_limit")
        values = row["values"]
        _require(type(values) is dict and set(values) == set(names), "row_feature_schema")
        vector = tuple(_number(values[name]) for name in names)
        gross, costs = _number(row["gross_return"]), _number(row["cost_fraction"])
        _require(Decimal(-1) <= gross <= 10 and 0 <= costs <= 1 and gross - costs >= -1,
                 "return_or_cost_bound")
        rows.append(vector)
        labels.append(Decimal(int(gross - costs > 0)))
        seen.add(row_id)
        pairs.add((subject, moment))
        used_sources.update(row_sources)
        windows[subject], previous = resolved, moment
    _require(used_sources == set(sources), "unused_declared_source")
    positives = sum(int(y) for y in labels)
    _require(min(positives, len(labels) - positives) >= MIN_CLASS_ROWS, "class_support")
    model = {key: data[key] for key in ("feature_names", "horizon_seconds",
        "maximum_feature_age_seconds", "cost_policy_id", "cost_policy_sha256")}
    model.update(schema=MODEL_SCHEMA, event=EVENT)
    dataset = TrainingDatasetManifest(data["dataset_id"], cutoff, len(rows), sources,
        _digest(raw), feature_schema_digest(names), label_schema_digest(model),
        data["cost_policy_id"], data["adjustment_policy_id"])
    return dataset, model, tuple(rows), tuple(labels), grants


def _objective_gradient(rows, labels, weights, intercept, l2):
    count = Decimal(len(rows))
    loss, gb = Decimal(0), Decimal(0)
    gradient = [l2 * w for w in weights]
    for x, y in zip(rows, labels):
        z = intercept + sum((w * v for w, v in zip(weights, x)), Decimal(0))
        _require(z.is_finite() and abs(z) <= 60, "logit_bound")
        p = Decimal(1) / (Decimal(1) + (-z).exp())
        loss += ((Decimal(1) + z.exp()).ln() - y * z) / count
        residual = (p - y) / count
        gb += residual
        for index, value in enumerate(x):
            gradient[index] += residual * value
    loss += l2 * sum((w * w for w in weights), Decimal(0)) / 2
    return loss, tuple(gradient), gb


def _fit(rows, labels, config):
    scales = tuple(max(Decimal(1), *(abs(row[j]) for row in rows))
                   for j in range(len(rows[0])))
    scaled = tuple(tuple(v / scale for v, scale in zip(row, scales)) for row in rows)
    weights, intercept = [Decimal(0)] * len(scales), Decimal(0)
    trace_bound = sum((sum((v*v for v in row), Decimal(0)) for row in scaled),
                      Decimal(0)) / len(scaled)
    step = 1 / ((1 + trace_bound) / 4 + config.l2)
    initial = None
    for iteration in range(config.maximum_iterations + 1):
        loss, gradient, gb = _objective_gradient(scaled, labels, weights, intercept, config.l2)
        initial = loss if initial is None else initial
        norm = max(abs(gb), *(abs(g) for g in gradient))
        _require(loss.is_finite() and norm.is_finite() and loss <= initial + Decimal("1e-24"),
                 "invalid_optimization")
        if norm <= config.gradient_tolerance:
            return tuple(w/s for w, s in zip(weights, scales)), intercept, {
                "iterations": iteration, "initial_objective": str(initial),
                "final_objective": str(loss), "gradient_infinity_norm": str(norm),
                "training_scales": [str(s) for s in scales], "converged": True}
        if iteration < config.maximum_iterations:
            weights = [w - step*g for w, g in zip(weights, gradient)]
            intercept -= step * gb
    raise ValueError("logistic_fit_not_converged")


def implementation_digest():
    parent = Path(__file__).parent
    return _digest(_canonical({name: _digest((parent/name).read_bytes()) for name in
        ("fitting.py", "training.py", "shadow.py", "contracts.py")}).encode())


def fit_candidate(raw, *, source_grants, run_id, candidate_id, config=None, clock=None):
    """Fit training data only. No holdout input, promotion or inference authorization."""
    config = FitConfig() if config is None else config
    _require(type(config) is FitConfig, "typed_config_required")
    config = replace(config)
    _identifier(run_id)
    _identifier(candidate_id)
    clock = clock or (lambda: datetime.now(timezone.utc))
    started = _instant(clock())
    code_digest = implementation_digest()
    with localcontext(Context(prec=34, rounding=ROUND_HALF_EVEN)):
        dataset, model, rows, labels, grants = _read_dataset(raw, source_grants, config, started)
        grant_payload = [{**asdict(g), "categories": sorted(v.value for v in g.categories),
            "planes": sorted(v.value for v in g.planes), "rights_status": g.rights_status.value}
            for g in sorted(grants, key=lambda value: value.source_id)]
        configuration = {"optimizer": config.payload(), "model": model, "source_grants": grant_payload}
        diagnostics = {}

        def trainer(_dataset, _seed):
            weights, intercept, detail = _fit(rows, labels, config)
            diagnostics.update(detail)
            return _canonical({**model, "coefficients": [str(w) for w in weights],
                               "intercept": str(intercept)}).encode()

        artifact, run = execute_training(dataset, run_id=run_id, candidate_id=candidate_id,
            model_family="decimal_logistic", code_sha256=code_digest,
            configuration_sha256=_digest(_canonical(configuration).encode()), seed=0,
            trainer=trainer, trained_at=started)
        ended = _instant(clock())
        _require(ended >= started, "completion_clock_reversed")
        _require(implementation_digest() == code_digest, "implementation_changed")
        bundle = ShadowModelBundle(dataset, replace(run, trained_at=ended), artifact)
        report = {"schema": FIT_SCHEMA, "mode": "TRAINING_ONLY", "trading_authorized": False,
            "out_of_sample_evaluated": False, "calibration_verified": False,
            "source_authenticity_verified": False, "rows": len(rows),
            "positive_after_cost_rows": sum(int(y) for y in labels),
            "started_at": started.isoformat(), "completed_at": ended.isoformat(),
            "dataset_sha256": dataset.source_snapshot_sha256,
            "artifact_sha256": run.artifact_sha256, "configuration": configuration,
            "optimization": diagnostics,
            "limitations": ["Caller-declared rights, source identities and availability times are not independently authenticated.",
                "Returns and costs are supplied observations, not verified broker fills or statutory rates.",
                "Optimization loss is in-sample; no holdout, calibration, trading edge or model approval is established.",
                "Non-overlapping rows per subject may still be dependent across subjects and time.",
                "One fit does not record a cumulative research trial count; governed evaluation and registration remain required."]}
        return FittedCandidate(bundle, _canonical(report))
