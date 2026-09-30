"""Synthetic rolling evaluation only; no provider requests or trading runtime."""
import hashlib
from datetime import timedelta
from decimal import Decimal

import pytest
from test_forecast_evaluation import NOW, T, grants, package

from quant_ai.learning.feature_dataset import canonical
from quant_ai.learning.rolling_evaluation import evaluate_rolling_forecasts


def folds():
    return [{"training_cutoff": T + timedelta(minutes=79, seconds=30),
             "holdout_start": T + timedelta(minutes=80), "holdout_end": T + timedelta(minutes=160)},
            {"training_cutoff": T + timedelta(minutes=159, seconds=30),
             "holdout_start": T + timedelta(minutes=160), "holdout_end": T + timedelta(minutes=240)}]


def evaluate(raw, boundaries=None):
    return evaluate_rolling_forecasts(raw, source_grants=grants(), folds=boundaries or folds(),
        run_id="synthetic", candidate_id="synthetic", clock=lambda: NOW)


def test_each_held_out_row_counted_once_with_fold_specific_training_baselines(tmp_path):
    result = evaluate(package(tmp_path, count=120))
    first, second = result["folds"]
    assert first["training"]["rows"] == 40
    assert second["training"]["rows"] == 80
    assert first["split"]["holdout_row_ids"] == [f"r{i}" for i in range(40, 80)]
    assert second["split"]["holdout_row_ids"] == [f"r{i}" for i in range(80, 120)]
    assert result["candidate"]["samples"] == 80
    for comparison in result["comparisons"].values():
        assert sum(comparison[k] for k in ("folds_better_brier", "folds_worse_brier", "folds_tied_brier")) == 2
    for flag in ("trading_authorized", "promotion_authorized", "forward_paper_evaluated", "calibration_verified"):
        assert result[flag] is False
    assert result["sha256"] == hashlib.sha256(canonical({k: v for k, v in result.items() if k != "sha256"})).hexdigest()


def test_late_training_label_is_purged_per_cutoff_and_high_cost_losses_retained(tmp_path):
    result = evaluate(package(tmp_path, count=120, late_label=True, costs=".1"))
    assert result["folds"][0]["training"]["rows"] == 39
    assert {"row_id": "r39", "reason": "label_unknown_at_training_cutoff"} in result["folds"][0]["split"]["excluded"]
    assert result["folds"][1]["training"]["rows"] == 80
    assert result["candidate"]["samples"] == 80
    assert Decimal(result["candidate"]["positive_rate"]) == 0


@pytest.mark.parametrize("change", ["overlap", "reverse", "future", "missing", "single"])
def test_invalid_fold_plans_refused_before_fitting(tmp_path, change):
    boundaries = folds()
    if change == "overlap": boundaries[1]["holdout_start"] = T + timedelta(minutes=159)
    if change == "reverse": boundaries.reverse()
    if change == "future": boundaries[1]["holdout_end"] = NOW + timedelta(seconds=1)
    if change == "missing": del boundaries[1]["holdout_end"]
    if change == "single": boundaries.pop()
    with pytest.raises(ValueError, match="forecast_evaluation_rolling"):
        evaluate(package(tmp_path, count=120), boundaries)


def test_holdout_outcomes_do_not_change_first_model_or_training_baseline(tmp_path):
    first = evaluate(package(tmp_path / "original", count=120))["folds"][0]
    changed = evaluate(package(tmp_path / "changed", count=120, reverse=True))["folds"][0]
    assert first["model_bundle"] == changed["model_bundle"]
    assert first["baselines"]["training_prevalence"]["mean_probability"] == changed["baselines"]["training_prevalence"]["mean_probability"]
    assert [p["probability_positive_after_cost"] for p in first["predictions"]] == [
        p["probability_positive_after_cost"] for p in changed["predictions"]]
    assert Decimal(changed["comparisons"]["constant_half"]["brier_improvement"]) < 0
