"""Read-only probability-drift observation; never turn consensus scores into probabilities.

An optional, private learning-monitor.json beside the decision-quality report names
one operator-declared tenant/candidate forecast journal and fixed evaluation windows.
It consumes the existing ForecastOutcomeJournal schema, does not create it, train a
model, approve a candidate, change risk settings or place orders. Configuring this
observer is not evidence that a production forecast producer has been activated.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import stat
from contextlib import closing
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from quant_ai.analytics.decision_journal import aware
from quant_ai.learning.drift import evaluate_probability_drift
from quant_ai.learning.outcomes import ForecastOutcomeJournal, ProbabilityForecast

SCHEMA = "pramana.learning_monitor.v1"
MIN_SAMPLES = 30
MAX_ROWS = 10000
FIELDS = frozenset({"schema", "tenant_id", "journal_filename", "candidate_id",
                    "model_artifact_sha256", "cost_policy_sha256", "reference_start",
                    "reference_end", "recent_start", "maximum_recent_age_seconds"})


def _unique(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise ValueError("monitor_duplicate_field")
        data[key] = value
    return data


def _hash(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _time(raw):
    return aware(datetime.fromisoformat(raw))


def _number(raw):
    value = Decimal(str(raw))
    if not value.is_finite():
        raise ValueError("monitor_nonfinite_value")
    return value


def _config(path, tenant, now):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > 16384):
        raise ValueError("monitor_config_not_private_regular_file")
    data = json.loads(path.read_text(), object_pairs_hook=_unique)
    if not isinstance(data, dict) or set(data) not in (FIELDS, FIELDS | {"require_shadow_lineage"}) or data["schema"] != SCHEMA:
        raise ValueError("monitor_config_schema_invalid")
    if "require_shadow_lineage" in data and data["require_shadow_lineage"] is not True:
        raise ValueError("monitor_shadow_requirement_must_be_true")
    if data["tenant_id"] != tenant:
        raise ValueError("monitor_tenant_mismatch")
    if not isinstance(data["candidate_id"], str) or not re.fullmatch(r"[A-Za-z0-9._:/-]{1,180}", data["candidate_id"]):
        raise ValueError("monitor_candidate_invalid")
    name = data["journal_filename"]
    if (not isinstance(name, str) or name in {".", ".."}
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,180}", name)):
        raise ValueError("monitor_journal_must_be_adjacent_file")
    for field in ("model_artifact_sha256", "cost_policy_sha256"):
        if not isinstance(data[field], str) or not re.fullmatch(r"[0-9a-f]{64}", data[field]):
            raise ValueError("monitor_artifact_binding_invalid")
    start, end, recent = (_time(data[name]) for name in ("reference_start", "reference_end", "recent_start"))
    if not start < end <= recent < now:
        raise ValueError("monitor_windows_must_be_time_ordered")
    age = data["maximum_recent_age_seconds"]
    if type(age) is not int or not 1 <= age <= 31536000:
        raise ValueError("monitor_recency_budget_invalid")
    return data, (start, end, recent)


def _validated_row(row, config, now):
    probability = _number(row["probability"])
    item = ProbabilityForecast(
        row["forecast_id"], row["pair_id"], row["candidate_id"], row["subject"], probability,
        _time(row["decision_at"]), _time(row["resolve_after"]), row["feature_snapshot_sha256"],
        row["model_artifact_sha256"], row["cost_policy_sha256"], row["regime"],
    )
    if (item.model_artifact_sha256 != config["model_artifact_sha256"]
            or item.cost_policy_sha256 != config["cost_policy_sha256"]):
        raise ValueError("monitor_mixed_artifact_or_cost_policy")
    if _hash(ForecastOutcomeJournal._forecast_payload(item)) != row["payload_sha256"]:
        raise ValueError("monitor_forecast_digest_mismatch")
    resolved = _time(row["resolved_at"])
    if not item.resolve_after <= resolved <= now:
        raise ValueError("monitor_outcome_time_invalid")
    gross, costs, net = (_number(row[key]) for key in ("gross_return", "cost_return", "after_cost_return"))
    if costs < 0 or gross - costs != net or row["positive_after_cost"] not in (0, 1):
        raise ValueError("monitor_outcome_arithmetic_invalid")
    if bool(row["positive_after_cost"]) != (net > 0):
        raise ValueError("monitor_target_mismatch")
    payload = {"forecastId": item.forecast_id, "resolvedAt": resolved.isoformat(),
               "grossReturn": str(gross), "costReturn": str(costs), "afterCostReturn": str(net),
               "positiveAfterCost": net > 0}
    if _hash(payload) != row["outcome_sha256"]:
        raise ValueError("monitor_outcome_digest_mismatch")
    return {**dict(row), "_decision": item.decision_at, "_resolved": resolved}


def _disjoint(rows):
    """Remove overlapping outcome intervals for each subject, not estimate independence."""
    selected, ends = [], {}
    for row in sorted(rows, key=lambda item: (item["_decision"], item["forecast_id"])):
        previous = ends.get(row["subject"])
        if previous is not None and row["_decision"] < previous:
            continue
        selected.append(row)
        ends[row["subject"]] = row["_resolved"]
    return selected


class _ReadOnlyPerformance(ForecastOutcomeJournal):
    def __init__(self, rows):
        self._rows = rows  # Deliberately do not construct/create any SQLite journal.

    def _resolved_rows(self, candidate_id):
        return [row for row in self._rows if row["candidate_id"] == candidate_id]


def observe_probability_drift(config_path, *, tenant_id, now):
    """Return unconfigured/insufficient/refused/healthy/degraded, with no authority change."""
    result = {"schema": SCHEMA, "status": "unconfigured", "healthy": None,
              "minimum_samples_per_window": MIN_SAMPLES, "reference_samples": 0,
              "recent_samples": 0, "reasons": [],
              "limitations": [
                  "Operator configuration declares tenant ownership; the source journal has no tenant column.",
                  "Non-overlapping per-subject windows do not establish cross-asset or session independence.",
                  "This monitors genuine after-cost probability forecasts, not raw consensus confidence.",
                  "A healthy drift check is not profitability, promotion or live-trading permission.",
              ]}
    path = Path(config_path)
    try:
        now = aware(now)
        result["checked_at"] = now.isoformat()
        if not path.exists() and not path.is_symlink():
            result["reasons"] = ["operator_monitor_configuration_missing"]
            return result
        config, (start, end, recent) = _config(path, tenant_id, now)
        journal = path.parent / config["journal_filename"]
        info = journal.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("monitor_journal_not_regular_file")
        result.update(config_sha256=_hash(config), candidate_id=config["candidate_id"])
        with closing(sqlite3.connect(journal.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("BEGIN")
            has_shadow = db.execute("SELECT 1 FROM sqlite_master WHERE name='shadow_journal_meta' AND type='table'").fetchone()
            if config.get("require_shadow_lineage") or has_shadow:
                from quant_ai.learning.shadow import validate_shadow_lineage

                result["shadow_lineage"] = validate_shadow_lineage(db, tenant_id=tenant_id)

            raw = db.execute("""SELECT f.*,o.resolved_at,o.gross_return,o.cost_return,
                o.after_cost_return,o.positive_after_cost,o.payload_sha256 AS outcome_sha256
                FROM probability_forecasts f JOIN forecast_outcomes o USING(forecast_id)
                WHERE f.candidate_id=? ORDER BY f.decision_at,f.forecast_id LIMIT ?""",
                             (config["candidate_id"], MAX_ROWS + 1)).fetchall()
        if len(raw) > MAX_ROWS:
            raise ValueError("monitor_row_limit_exceeded")
        rows = [_validated_row(row, config, now) for row in raw]
        if (len({row["forecast_id"] for row in rows}) != len(rows)
                or len({row["pair_id"] for row in rows}) != len(rows)):
            raise ValueError("monitor_duplicate_forecast_identity")
        reference_raw = [row for row in rows if start <= row["_decision"] < end and row["_resolved"] <= end]
        recent_raw = [row for row in rows if recent <= row["_decision"] and row["_resolved"] <= now]
        reference, current = _disjoint(reference_raw), _disjoint(recent_raw)
        result.update(reference_raw=len(reference_raw), recent_raw=len(recent_raw),
                      reference_samples=len(reference), recent_samples=len(current),
                      excluded_overlaps=len(reference_raw) + len(recent_raw) - len(reference) - len(current))
        if len(reference) < MIN_SAMPLES or len(current) < MIN_SAMPLES:
            result.update(status="insufficient_sample", reasons=["insufficient_eligible_nonoverlapping_forecasts"])
            return result
        if now - max(row["_resolved"] for row in current) > timedelta(seconds=config["maximum_recent_age_seconds"]):
            raise ValueError("monitor_recent_evidence_stale")
        before = _ReadOnlyPerformance(reference).performance(config["candidate_id"], min_samples=MIN_SAMPLES)
        after = _ReadOnlyPerformance(current).performance(config["candidate_id"], min_samples=MIN_SAMPLES)
        decision = evaluate_probability_drift(before, after)
        result.update(status="healthy" if decision.healthy else "degraded", healthy=decision.healthy,
                      reasons=list(decision.reasons),
                      reference_brier=str(before.brier_score), recent_brier=str(after.brier_score),
                      reference_ece=str(before.expected_calibration_error), recent_ece=str(after.expected_calibration_error),
                      recent_after_cost_expectancy=str(after.mean_after_cost_return),
                      evidence_sha256=_hash([(row["forecast_id"], row["payload_sha256"], row["outcome_sha256"])
                                             for row in reference + current]))
        return result
    except (OSError, ValueError, TypeError, KeyError, ArithmeticError, sqlite3.Error) as error:
        # Never serialize a file path, database message or arbitrary configuration content.
        result.update(status="refused", healthy=None, reasons=["invalid_or_unavailable_monitor_evidence"],
                      error_type=type(error).__name__)
        return result


def enrich_learning_report(report, engine, *, config_path, tenant_id, now):
    """Reuse the existing report and its displayed limitations; no duplicate dashboard."""
    feedback = dict(getattr(engine, "feedback", {"status": "not_refreshed"}))
    if feedback.get("tenant_id") not in {None, tenant_id}:
        feedback = {"status": "refused", "credited_entries": 0, "reason": "feedback_tenant_mismatch"}
    drift = observe_probability_drift(config_path, tenant_id=tenant_id, now=now)
    report["learning_feedback"] = feedback
    report["probability_drift"] = drift
    report["limitations"].extend([
        f"Specialist feedback: {feedback.get('status')}; {feedback.get('credited_entries', 0)} audited closed entries. Not proof of improved skill.",
        f"Probability drift: {drift['status']}; reference/recent eligible samples {drift['reference_samples']}/{drift['recent_samples']}. No automatic promotion.",
    ])
    return drift
