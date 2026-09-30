"""Bounded retrospective expanding-window evaluation; no trading or promotion."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from pathlib import Path

from quant_ai.learning.feature_dataset import canonical
from quant_ai.learning.forecast_evaluation import _check, _digest, _metrics, evaluate_forecasts
from quant_ai.learning.shadow import _instant


def evaluate_rolling_forecasts(package_raw, *, source_grants, folds, run_id, candidate_id, clock=None):
    """Evaluate every declared fold without selecting a winner or modifying a model.

    Test windows are half-open and non-overlapping. Training expands; earlier test
    outcomes may enter later training only once available at that later cutoff.
    Fold boundaries must be frozen before inspecting results by the caller.
    """
    _check(type(folds) is list and 2 <= len(folds) <= 12, "rolling_fold_count")
    clock = clock or (lambda: datetime.now(timezone.utc))
    started = _instant(clock())
    normalized = []
    for fold in folds:
        _check(type(fold) is dict and set(fold) == {"training_cutoff", "holdout_start", "holdout_end"},
               "rolling_fold_fields")
        cutoff, start, end = (_instant(fold[k]) for k in
                              ("training_cutoff", "holdout_start", "holdout_end"))
        _check(cutoff < start < end <= started, "rolling_split_order")
        if normalized:
            prior = normalized[-1]
            _check(cutoff > prior[0] and start >= prior[2], "rolling_overlap_or_order")
        normalized.append((cutoff, start, end))
    reports = [evaluate_forecasts(package_raw, source_grants=source_grants,
        training_cutoff=cutoff, holdout_start=start, holdout_end=end,
        run_id=f"{run_id}-fold-{i}", candidate_id=f"{candidate_id}-fold-{i}", clock=clock)
        for i, (cutoff, start, end) in enumerate(normalized)]
    predictions = [p for report in reports for p in report["predictions"]]
    _check(len({p["row_id"] for p in predictions}) == len(predictions), "rolling_duplicate_test_row")
    with localcontext(Context(prec=34, rounding=ROUND_HALF_EVEN)):
        targets = [Decimal(int(p["positive_after_cost"])) for p in predictions]
        candidate = _metrics([Decimal(p["probability_positive_after_cost"]) for p in predictions], targets)
        baselines = {name: _metrics([Decimal(r["baselines"][name]["mean_probability"])
            for r in reports for _ in r["predictions"]], targets)
            for name in ("constant_half", "training_prevalence")}
        comparisons = {name: {
            "brier_improvement": str(Decimal(metrics["brier_score"]) - Decimal(candidate["brier_score"])),
            "log_loss_improvement": str(Decimal(metrics["log_loss"]) - Decimal(candidate["log_loss"])),
            "folds_better_brier": sum(Decimal(r["comparisons"][name]["brier_improvement"]) > 0 for r in reports),
            "folds_worse_brier": sum(Decimal(r["comparisons"][name]["brier_improvement"]) < 0 for r in reports),
            "folds_tied_brier": sum(Decimal(r["comparisons"][name]["brier_improvement"]) == 0 for r in reports),
        } for name, metrics in baselines.items()}
    completed = _instant(clock())
    _check(completed >= started, "clock_reversed")
    result = {"schema": "pramana.forecast_rolling_evaluation.v1", "mode": "RETROSPECTIVE_ROLLING",
        "training_window": "expanding", "started_at": started.isoformat(), "completed_at": completed.isoformat(),
        "package_sha256": _digest(package_raw), "evaluation_code_sha256": _digest(Path(__file__).read_bytes()),
        "folds": reports, "candidate": candidate, "baselines": baselines, "comparisons": comparisons,
        "trading_authorized": False, "promotion_authorized": False, "forward_paper_evaluated": False,
        "calibration_verified": False, "source_authenticity_verified": False,
        "limitations": ["Retrospective, not forecasts recorded prospectively; no live-readiness verdict.",
            "Caller supplies frozen boundaries and source rights; repeated research selection is not corrected.",
            "Pooled calibration counts each held-out row once; dependence remains and no confidence claim is made.",
            "Recorded after-cost opportunity outcomes are not realized execution or portfolio P&L.",
            "No model is selected, promoted, or deployed; existing execution and risk gates are untouched."]}
    result["sha256"] = _digest(canonical(result))
    return result
