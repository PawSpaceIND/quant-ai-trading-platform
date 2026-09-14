"""Offline forecast diagnostics. No model training, promotion or order execution.

Probability means P(net return > 0 at a predeclared horizon/cost policy), NOT an
LLM's generic confidence score. Historical rows are supplied evidence, not verified
forward records; caller must retain their immutable source and trial registry.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone


def _instant(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("timezone_required")
    return result.astimezone(timezone.utc)


def _prob(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("invalid_probability")
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("invalid_probability")
    return value


def evaluate_forecasts(protocol, records, *, as_of):
    """All scheduled cases, including errors, must be present; absence stays visible.

    Brier scores are paired on resolved predictions only. Coverage and error counts
    include the full declared schedule. No statistical significance or edge claimed.
    """
    now = _instant(as_of)
    for key in ("protocol_id", "candidate_version", "cost_policy_id", "outcome_definition"):
        if not isinstance(protocol[key], str) or not protocol[key].strip():
            raise ValueError("protocol_identity_required")
    if protocol["outcome_definition"] != "net_return_positive":
        raise ValueError("unsupported_outcome_definition")
    horizon = protocol["horizon_seconds"]
    if type(horizon) is not int or not 0 < horizon <= 366 * 86400:
        raise ValueError("invalid_horizon")
    baseline = _prob(protocol["baseline_probability"])
    threshold = _prob(protocol["abstain_below_probability"])
    if threshold < 0.5:
        raise ValueError("threshold_below_half")
    scheduled = protocol["scheduled_case_ids"]
    if (not isinstance(scheduled, list) or not scheduled or len(scheduled) > 10000
            or any(not isinstance(x, str) or not x for x in scheduled)
            or len(set(scheduled)) != len(scheduled)):
        raise ValueError("unique_schedule_required")
    frozen = _instant(protocol["frozen_at"])
    seen = set()
    errors = abstentions = unresolved = forecasts = 0
    pairs = []
    for row in records:
        case = row["case_id"]
        if case in seen or case not in scheduled:
            raise ValueError("duplicate_or_unscheduled_case")
        seen.add(case)
        decision = _instant(row["decided_at"])
        if not frozen <= decision <= now or _instant(row["input_available_at"]) > decision:
            raise ValueError("future_or_unfrozen_input")
        if row["candidate_version"] != protocol["candidate_version"]:
            raise ValueError("candidate_drift")
        status = row["status"]
        if status not in ("forecast", "abstain", "provider_error"):
            raise ValueError("invalid_status")
        if status != "forecast":
            if row.get("probability") is not None:
                raise ValueError("non_forecast_probability")
            errors += status == "provider_error"
            abstentions += status == "abstain"
            continue
        forecasts += 1
        probability = _prob(row["probability"])
        outcome = row.get("outcome")
        if outcome is None:
            unresolved += 1
            continue
        if outcome["cost_policy_id"] != protocol["cost_policy_id"]:
            raise ValueError("cost_policy_drift")
        measured = _instant(outcome["measured_at"])
        available = _instant(outcome["available_at"])
        if measured != decision + timedelta(seconds=horizon) or not measured <= available <= now:
            raise ValueError("invalid_outcome_horizon_or_availability")
        net = outcome["net_return"]
        if isinstance(net, bool) or not isinstance(net, (float, int)) or not math.isfinite(net):
            raise ValueError("invalid_net_return")
        pairs.append((probability, int(net > 0), net))
    n = len(pairs)
    selected = [r for r in pairs if r[0] >= threshold]
    bins = []
    for i in range(10):
        group = [r for r in pairs if min(int(r[0] * 10), 9) == i]
        bins.append({"lower": i / 10, "upper": (i + 1) / 10, "count": len(group),
                     "mean_probability": sum(r[0] for r in group) / len(group) if group else None,
                     "positive_fraction": sum(r[1] for r in group) / len(group) if group else None})
    return {
        "protocol_id": protocol["protocol_id"], "candidate_version": protocol["candidate_version"],
        "diagnostic_only": True, "promotion_approved": False,
        "scheduled": len(scheduled), "missing": len(scheduled) - len(seen),
        "provider_errors": errors, "explicit_abstentions": abstentions,
        "forecasts": forecasts, "resolved": n, "unresolved": unresolved,
        "forecast_coverage": forecasts / len(scheduled),
        "resolved_coverage": n / len(scheduled),
        "brier": sum((p - y)**2 for p, y, _ in pairs) / n if n else None,
        "paired_baseline_brier": sum((baseline - y)**2 for _, y, _ in pairs) / n if n else None,
        "threshold": threshold, "threshold_selected_resolved": len(selected),
        "threshold_positive_fraction": sum(r[1] for r in selected) / len(selected) if selected else None,
        "reliability_bins": bins,
        "limitations": ["supplied_evidence_not_forward_attestation", "not_a_portfolio_backtest",
                        "selection_bias_not_corrected", "no_statistical_significance_claim"],
    }


def validate_temporal_split(training_windows, evaluation_windows, *, embargo_seconds):
    """Require every training outcome before evaluation input, with a fixed embargo.

    Windows cover input availability through outcome availability. This conservative
    walk-forward check rejects future training and any cross-set overlapping labels.
    It does not detect point-in-time data revisions or unreported tuning trials.
    """
    if type(embargo_seconds) is not int or embargo_seconds < 0:
        raise ValueError("invalid_embargo")
    if not training_windows or not evaluation_windows:
        raise ValueError("both_sets_required")

    def parse(windows):
        parsed = [(_instant(start), _instant(end)) for start, end in windows]
        if any(start > end for start, end in parsed):
            raise ValueError("inverted_window")
        return parsed

    training, evaluation = parse(training_windows), parse(evaluation_windows)
    if max(end for _, end in training) + timedelta(seconds=embargo_seconds) >= min(
        start for start, _ in evaluation
    ):
        raise ValueError("training_overlaps_evaluation_or_embargo")
    return {"valid": True, "training_windows": len(training), "evaluation_windows": len(evaluation)}


def assess_forecast_input(*, probability, threshold, decided_at, quote_at,
                          max_quote_age_seconds, evidence_available_at,
                          adjustment_verified, provider_ok):
    """Research abstention filter; acceptance never means authorization to trade.

    Pass probability=None for a generic confidence score or unavailable calibrated
    forecast. Data licensing and deterministic portfolio risk are separate checks.
    """
    threshold = _prob(threshold)
    if threshold < 0.5:
        raise ValueError("threshold_below_half")
    if type(max_quote_age_seconds) is not int or max_quote_age_seconds <= 0:
        raise ValueError("invalid_quote_age_limit")
    if type(adjustment_verified) is not bool or type(provider_ok) is not bool:
        raise TypeError("boolean_required")
    decision, quote = _instant(decided_at), _instant(quote_at)
    reasons = []
    if not provider_ok:
        reasons.append("provider_unavailable")
    if probability is None:
        reasons.append("probability_unavailable")
    elif _prob(probability) < threshold:
        reasons.append("below_frozen_threshold")
    if not timedelta(0) <= decision - quote <= timedelta(seconds=max_quote_age_seconds):
        reasons.append("stale_or_future_quote")
    if not isinstance(evidence_available_at, (list, tuple)) or not evidence_available_at:
        reasons.append("evidence_missing")
    elif any(_instant(at) > decision for at in evidence_available_at):
        reasons.append("future_evidence")
    if not adjustment_verified:
        reasons.append("corporate_action_adjustment_unverified")
    return {"abstain": bool(reasons), "reasons": reasons, "order_authorized": False}
