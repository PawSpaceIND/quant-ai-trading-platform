from copy import deepcopy

import pytest

from quant_ai.research.calibration import evaluate_forecasts, validate_temporal_split

NOW = "2026-09-14T15:00:00+00:00"


def protocol():
    return {"protocol_id": "p1", "candidate_version": "model-prompt-v1", "cost_policy_id": "fees-v1",
            "outcome_definition": "net_return_positive", "horizon_seconds": 3600,
            "baseline_probability": 0.5, "abstain_below_probability": 0.8,
            "scheduled_case_ids": ["a", "b", "c", "d", "e"], "frozen_at": "2026-09-14T09:00:00Z"}


def forecast(case="a", probability=0.8, net=0.01):
    return {"case_id": case, "candidate_version": "model-prompt-v1", "status": "forecast",
            "decided_at": "2026-09-14T10:00:00Z", "input_available_at": "2026-09-14T09:59:00Z",
            "probability": probability, "outcome": {"net_return": net,
            "measured_at": "2026-09-14T11:00:00Z", "available_at": "2026-09-14T11:01:00Z",
            "cost_policy_id": "fees-v1"}}


def test_paired_brier_bins_and_all_schedule_coverage():
    rows = [forecast(), forecast("b", 0.2, -0.02), forecast("c")]
    rows[2].update(status="provider_error", probability=None, outcome=None)
    rows.append({**forecast("d"), "status": "abstain", "probability": None, "outcome": None})
    r = evaluate_forecasts(protocol(), rows, as_of=NOW)
    assert r["brier"] == pytest.approx(0.04)
    assert r["paired_baseline_brier"] == 0.25
    assert r["forecast_coverage"] == 0.4
    assert (r["missing"], r["provider_errors"], r["explicit_abstentions"]) == (1, 1, 1)
    assert r["threshold_selected_resolved"] == 1
    assert r["threshold_positive_fraction"] == 1
    assert sum(b["count"] for b in r["reliability_bins"]) == 2
    assert r["promotion_approved"] is False


def test_unresolved_outcome_is_not_a_loss_or_success():
    row = forecast()
    row["outcome"] = None
    r = evaluate_forecasts(protocol(), [row], as_of=NOW)
    assert r["unresolved"] == 1
    assert r["brier"] is None
    assert r["threshold_positive_fraction"] is None
    assert r["resolved_coverage"] == 0


def test_zero_return_is_not_positive_and_probability_one_has_bin():
    r = evaluate_forecasts(protocol(), [forecast(probability=1, net=0)], as_of=NOW)
    assert r["brier"] == 1
    assert r["reliability_bins"][-1]["count"] == 1


@pytest.mark.parametrize("prob", [float("nan"), float("inf"), -0.1, 1.1, True, "0.8"])
def test_invalid_probabilities_fail(prob):
    with pytest.raises((ValueError, TypeError), match="probability"):
        evaluate_forecasts(protocol(), [forecast(probability=prob)], as_of=NOW)


@pytest.mark.parametrize("field,value", [
    ("decided_at", "2026-09-14T08:00:00Z"),
    ("input_available_at", "2026-09-14T10:01:00Z"),
    ("candidate_version", "other"), ("status", "buy"),
])
def test_input_and_version_leakage_rejected(field, value):
    row = forecast()
    row[field] = value
    with pytest.raises(ValueError):
        evaluate_forecasts(protocol(), [row], as_of=NOW)


@pytest.mark.parametrize("field,value", [
    ("measured_at", "2026-09-14T12:00:00Z"),
    ("available_at", "2026-09-14T16:00:00Z"),
    ("cost_policy_id", "cheaper-costs"), ("net_return", float("nan")),
])
def test_outcome_selection_and_cost_drift_rejected(field, value):
    row = forecast()
    row["outcome"][field] = value
    with pytest.raises(ValueError):
        evaluate_forecasts(protocol(), [row], as_of=NOW)


def test_duplicate_cases_and_unknown_cases_fail():
    with pytest.raises(ValueError, match="duplicate_or_unscheduled"):
        evaluate_forecasts(protocol(), [forecast(), forecast()], as_of=NOW)
    with pytest.raises(ValueError, match="duplicate_or_unscheduled"):
        evaluate_forecasts(protocol(), [forecast("unknown")], as_of=NOW)


def test_input_objects_unchanged_and_no_automatic_promotion():
    p, rows = protocol(), [forecast()]
    before = deepcopy((p, rows))
    r = evaluate_forecasts(p, rows, as_of=NOW)
    assert (p, rows) == before
    assert r["diagnostic_only"] and not r["promotion_approved"]


def test_conservative_walk_forward_purge_and_embargo():
    train = [("2026-09-01T09:00:00Z", "2026-09-01T10:00:00Z")]
    test = [("2026-09-01T10:10:01Z", "2026-09-01T11:00:00Z")]
    assert validate_temporal_split(train, test, embargo_seconds=600)["valid"]
    with pytest.raises(ValueError, match="overlaps"):
        validate_temporal_split(train, test, embargo_seconds=601)
    with pytest.raises(ValueError, match="overlaps"):
        validate_temporal_split(test, train, embargo_seconds=0)
    with pytest.raises(ValueError, match="both_sets"):
        validate_temporal_split([], test, embargo_seconds=0)


def test_abstention_checks_real_input_quality_and_never_authorizes_orders():
    from quant_ai.research.calibration import assess_forecast_input

    inputs = {"probability": 0.9, "threshold": 0.8, "decided_at": "2026-09-14T10:00:00Z",
              "quote_at": "2026-09-14T09:59:50Z", "max_quote_age_seconds": 30,
              "evidence_available_at": ["2026-09-14T09:59:00Z"],
              "adjustment_verified": True, "provider_ok": True}
    assert assess_forecast_input(**inputs) == {
        "abstain": False, "reasons": [], "order_authorized": False,
    }
    for changes, reason in [
        ({"probability": None}, "probability_unavailable"),
        ({"probability": 0.7}, "below_frozen_threshold"),
        ({"provider_ok": False}, "provider_unavailable"),
        ({"quote_at": "2026-09-14T09:58:00Z"}, "stale_or_future_quote"),
        ({"quote_at": "2026-09-14T10:00:01Z"}, "stale_or_future_quote"),
        ({"evidence_available_at": []}, "evidence_missing"),
        ({"evidence_available_at": ["2026-09-14T10:00:01Z"]}, "future_evidence"),
        ({"adjustment_verified": False}, "corporate_action_adjustment_unverified"),
    ]:
        report = assess_forecast_input(**{**inputs, **changes})
        assert report["abstain"] and reason in report["reasons"]
        assert not report["order_authorized"]


def test_calibration_cli_executes_real_scoring(tmp_path, capsys):
    import json

    from quant_ai.research.saas_evidence import main

    path = tmp_path / "forecasts.json"
    path.write_text(json.dumps({"protocol": protocol(), "records": [forecast()]}))
    main(["calibration", "--input", str(path), "--as-of", NOW])
    report = json.loads(capsys.readouterr().out)["report"]
    assert report["brier"] == pytest.approx(0.04)
    assert report["missing"] == 4
