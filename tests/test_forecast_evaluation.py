"""Synthetic chronology tests: results demonstrate evaluator behavior, not market skill."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from decimal import Context, localcontext
from decimal import Decimal as D

import pytest
from test_feature_training_dataset import T, build, grants, plan, rehash

from quant_ai.features.store import FeatureObservation, PointInTimeFeatureStore
from quant_ai.learning import forecast_evaluation as evaluation
from quant_ai.learning.feature_dataset import canonical
from quant_ai.learning.outcomes import ForecastOutcomeJournal, ProbabilityForecast
from quant_ai.learning.shadow import ShadowModelBundle, predict

NOW = T + timedelta(days=1)
CUTOFF = T + timedelta(minutes=79, seconds=30)
START = T + timedelta(minutes=80)


def package(tmp_path, *, reverse=False, costs="0.001", count=80, late_label=False,
            single_class_test=False, test_signal=None):
    path = tmp_path / "source.db"
    p = plan(count)
    with PointInTimeFeatureStore(path) as store:
        for i, decision in enumerate(p["decisions"]):
            at = datetime.fromisoformat(decision["decision_at"])
            positive = i % 2 == 0
            if i >= 40 and (reverse or single_class_test):
                positive = not positive if reverse else True
            due = at + timedelta(seconds=60)
            known = due + timedelta(seconds=2)
            if late_label and i == 39:
                known = T + timedelta(minutes=90)
            signal = "1" if i % 2 == 0 else "-1"
            if i >= 40 and test_signal is not None:
                signal = test_signal
            for rule, value, observed, available, suffix in (
                (p["features"][0], signal, at, at, "feature"),
                (p["price"], "100", at, at, "entry"),
                (p["cost"], costs if i >= 40 else "0.001", at, at, "cost"),
                (p["price"], "102" if positive else "99", due, known, "exit"),
            ):
                store.append(FeatureObservation(f"{i}-{suffix}", decision["subject"],
                    rule["feature"], D(value), observed, available, "recorded", rule["schema_id"]))
    return build(path, p)


def evaluate(raw, **overrides):
    options = {"source_grants": grants(), "training_cutoff": CUTOFF, "holdout_start": START,
               "run_id": "synthetic-evaluation", "candidate_id": "synthetic-candidate", "clock": lambda: NOW}
    options.update(overrides)
    return evaluation.evaluate_forecasts(raw, **options)


def test_fits_only_earlier_rows_and_scores_every_later_opportunity(tmp_path):
    raw = package(tmp_path)
    report = evaluate(raw)
    assert report["training"]["rows"] == 40
    assert report["candidate"]["samples"] == 40
    assert report["split"]["training_row_ids"] == [f"r{i}" for i in range(40)]
    assert report["split"]["holdout_row_ids"] == [f"r{i}" for i in range(40, 80)]
    assert report["split"]["excluded"] == []
    assert report["input_rows"] == 80
    assert D(report["candidate"]["brier_score"]) < D("0.25")
    assert all(D(v["brier_improvement"]) > 0 for v in report["comparisons"].values())
    assert report["mode"] == "RETROSPECTIVE_HOLDOUT"
    assert report["out_of_sample_evaluated"] is True
    for flag in ("trading_authorized", "promotion_authorized", "forward_paper_evaluated",
                 "calibration_verified", "source_authenticity_verified"):
        assert report[flag] is False
    assert report["package_sha256"] == hashlib.sha256(raw).hexdigest()
    assert report["sha256"] == hashlib.sha256(canonical({k: v for k, v in report.items()
                                                        if k != "sha256"})).hexdigest()
    assert report == evaluate(raw)


def test_holdout_regime_reversal_cannot_change_fitted_model_or_baseline(tmp_path):
    original = evaluate(package(tmp_path / "good"))
    reversed_report = evaluate(package(tmp_path / "reversed", reverse=True))
    assert original["model_bundle"]["artifact_text"] == reversed_report["model_bundle"]["artifact_text"]
    assert original["training"]["dataset_sha256"] == reversed_report["training"]["dataset_sha256"]
    assert original["model_bundle"] == reversed_report["model_bundle"]
    assert original["training"]["optimization"] == reversed_report["training"]["optimization"]
    assert [p["probability_positive_after_cost"] for p in original["predictions"]] == [
        p["probability_positive_after_cost"] for p in reversed_report["predictions"]]
    assert all(D(v["brier_improvement"]) < 0 for v in reversed_report["comparisons"].values())
    assert reversed_report["candidate"]["samples"] == 40  # Losing cases are not dropped.


def test_training_base_rate_does_not_peek_at_holdout_class_balance(tmp_path):
    report = evaluate(package(tmp_path, single_class_test=True))
    baseline = report["baselines"]["training_prevalence"]
    assert D(baseline["mean_probability"]) == D("0.5")
    assert D(baseline["positive_rate"]) == 1
    assert D(baseline["brier_score"]) == D("0.25")


def test_holdout_outliers_cannot_leak_into_training_scaling(tmp_path):
    original = evaluate(package(tmp_path / "original"))
    outlier = evaluate(package(tmp_path / "outlier", test_signal="10"))
    assert outlier["model_bundle"] == original["model_bundle"]
    assert outlier["training"]["optimization"]["training_scales"] == ["1"]
    assert outlier["predictions"][0]["probability_positive_after_cost"] != original["predictions"][0][
        "probability_positive_after_cost"]


def test_unsupported_holdout_input_refuses_instead_of_skipping_a_test_row(tmp_path):
    raw = package(tmp_path, test_signal="1000")
    with pytest.raises(ValueError, match="logit_outside_supported_domain"):
        evaluate(raw)


def test_recorded_costs_can_turn_price_gains_into_failed_predictions(tmp_path):
    report = evaluate(package(tmp_path, costs="0.03"))
    assert all(not p["positive_after_cost"] for p in report["predictions"])
    assert {D(p["recorded_after_cost_return"]) for p in report["predictions"]} == {D("-.01"), D("-.04")}
    assert report["candidate"]["positive_rate"] == "0"


def test_late_training_labels_are_purged_and_embargo_rows_are_accounted_for(tmp_path):
    raw = package(tmp_path, late_label=True)
    report = evaluate(raw, holdout_start=START + timedelta(minutes=4))
    assert report["training"]["rows"] == 39
    assert report["candidate"]["samples"] == 38
    assert report["split"]["excluded"] == [
        {"row_id": "r39", "reason": "label_unknown_at_training_cutoff"},
        {"row_id": "r40", "reason": "embargo"}, {"row_id": "r41", "reason": "embargo"}]
    assert report["input_rows"] == 39 + 38 + 3


def test_same_time_and_overlapping_training_cutoffs_are_rejected(tmp_path):
    raw = package(tmp_path)
    with pytest.raises(ValueError, match="split_order"):
        evaluate(raw, training_cutoff=START)
    with pytest.raises(ValueError, match="split_order"):
        evaluate(raw, training_cutoff=START + timedelta(seconds=1))
    with pytest.raises(ValueError, match="holdout_after_dataset"):
        evaluate(raw, holdout_start=T + timedelta(hours=5))


def test_insufficient_test_or_training_rows_refuse_instead_of_claiming_skill(tmp_path):
    raw = package(tmp_path)
    with pytest.raises(ValueError, match="holdout_sample_too_small"):
        evaluate(raw, holdout_start=T + timedelta(minutes=120))
    with pytest.raises(ValueError, match="row_count"):
        evaluate(raw, training_cutoff=T + timedelta(minutes=30))


def test_rehashed_forged_holdout_label_is_rejected_by_source_replay(tmp_path):
    payload = json.loads(package(tmp_path))
    payload["data"]["rows"][-1]["gross_return"] = "0.9"
    with pytest.raises(ValueError, match="lineage_replay"):
        evaluate(rehash(payload))


def test_historical_evaluation_does_not_backdate_or_bypass_forward_model_time(tmp_path):
    raw = package(tmp_path)
    report = evaluate(raw)
    bundle = ShadowModelBundle.from_payload(report["model_bundle"])
    row = json.loads(raw)["data"]["rows"][40]
    vector = {"schema": "pramana.numeric_features.v1", "subject": row["subject"],
        "observed_at": row["observed_at"], "available_at": row["available_at"],
        "source_ids": row["source_ids"], "values": row["values"]}
    assert bundle.training_run.trained_at == NOW
    with pytest.raises(ValueError, match="training_time"):
        predict(bundle, vector, now=START)
    vector.update(observed_at=NOW.isoformat(), available_at=NOW.isoformat())
    probability = predict(bundle, vector, now=NOW)[0]
    assert probability == D(report["predictions"][0]["probability_positive_after_cost"])


def test_metrics_match_existing_probability_outcome_scoring(tmp_path):
    report = evaluate(package(tmp_path))
    with ForecastOutcomeJournal(":memory:") as journal, localcontext(Context(prec=34)):
        for p in report["predictions"]:
            at, due = datetime.fromisoformat(p["decision_at"]), datetime.fromisoformat(p["resolve_after"])
            journal.record_forecast(ProbabilityForecast(p["row_id"], p["row_id"], "test", p["subject"],
                D(p["probability_positive_after_cost"]), at, due, "a"*64, "b"*64, "c"*64))
            journal.resolve(p["row_id"], gross_return=D(p["recorded_after_cost_return"]),
                            cost_return=D(0), resolved_at=NOW)
        metrics = journal.performance("test")
        for key in ("brier_score", "log_loss", "expected_calibration_error", "positive_rate"):
            assert D(report["candidate"][key]) == getattr(metrics, key)


def test_public_evaluation_refuses_reversing_clock(tmp_path):
    raw = package(tmp_path)
    times = iter([NOW, NOW, NOW, NOW - timedelta(seconds=1)])
    with pytest.raises(ValueError, match="clock_reversed"):
        evaluate(raw, clock=lambda: next(times))
