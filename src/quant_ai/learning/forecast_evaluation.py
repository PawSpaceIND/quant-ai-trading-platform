"""Retrospective chronological evaluation of recorded, source-backed observations.

Only the earlier partition reaches the fitter. Later observations are scored with
the same bounded arithmetic as shadow inference, without backdating model creation
or writing historical predictions into a forward forecast journal.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from pathlib import Path

from quant_ai.learning.feature_dataset import canonical, validate_training_package
from quant_ai.learning.fitting import fit_candidate
from quant_ai.learning.outcomes import ForecastOutcomeJournal
from quant_ai.learning.shadow import _instant, _numeric_probability

SCHEMA = "pramana.forecast_holdout_evaluation.v1"
MIN_TEST_ROWS = 30


def _check(condition, reason):
    if not condition:
        raise ValueError("forecast_evaluation_" + reason)


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _metrics(probabilities, targets):
    count = Decimal(len(targets))
    bins = ForecastOutcomeJournal._calibration_bins(probabilities, targets)
    ln = ForecastOutcomeJournal._ln_probability
    return {
        "samples": len(targets),
        "brier_score": str(sum(((p - y) ** 2 for p, y in zip(probabilities, targets)),
                                Decimal(0)) / count),
        "log_loss": str(sum((-(y * ln(p) + (1 - y) * ln(1 - p))
                             for p, y in zip(probabilities, targets)), Decimal(0)) / count),
        "expected_calibration_error": str(sum((Decimal(b.count) / count *
            abs(b.mean_probability - b.positive_rate) for b in bins), Decimal(0))),
        "mean_probability": str(sum(probabilities, Decimal(0)) / count),
        "positive_rate": str(sum(targets, Decimal(0)) / count),
        "calibration_bins": [{k: str(v) if isinstance(v, Decimal) else v
                              for k, v in asdict(b).items()} for b in bins],
    }


def evaluate_forecasts(package_raw, *, source_grants, training_cutoff, holdout_start,
                       run_id, candidate_id, clock=None):
    """Fit earlier rows and evaluate every later row; no model selection or promotion.

    The existing package is a recorded-observation container, not permission to fit
    all of its rows. The explicit split always creates a separate training dataset.
    Rows whose labels were unavailable at training cutoff are purged, never relabelled.
    """
    clock = clock or (lambda: datetime.now(timezone.utc))
    started = _instant(clock())
    cutoff, first_test = _instant(training_cutoff), _instant(holdout_start)
    _check(cutoff < first_test <= started, "split_order")
    data = json.loads(validate_training_package(package_raw, source_grants=source_grants,
                                               now=started))
    _check(first_test <= _instant(data["cutoff"]), "holdout_after_dataset")
    training, holdout, excluded = [], [], []
    for row in data["rows"]:
        at = _instant(row["decision_at"])
        if at <= cutoff:
            if _instant(row["outcome_available_at"]) <= cutoff:
                training.append(row)
            else:
                excluded.append({"row_id": row["row_id"], "reason": "label_unknown_at_training_cutoff"})
        elif at < first_test:
            excluded.append({"row_id": row["row_id"], "reason": "embargo"})
        else:
            holdout.append(row)
    _check(len(holdout) >= MIN_TEST_ROWS, "holdout_sample_too_small")
    split = {"training_cutoff": cutoff.isoformat(), "holdout_start": first_test.isoformat(),
             "training_row_ids": [r["row_id"] for r in training],
             "holdout_row_ids": [r["row_id"] for r in holdout], "excluded": excluded}
    package_sha = _digest(package_raw)
    training_data = {**data, "cutoff": cutoff.isoformat(), "rows": training}
    # Even the training identity depends only on training observations and metadata.
    # The complete package and held-out partition are bound separately in the report.
    training_data["dataset_id"] = "evaluation-training-" + _digest(canonical(training_data))
    fitted = fit_candidate(canonical(training_data), source_grants=source_grants,
                           run_id=run_id, candidate_id=candidate_id, clock=clock)
    model = fitted.bundle.validate()
    with localcontext(Context(prec=34, rounding=ROUND_HALF_EVEN)):
        prevalence = sum((Decimal(r["gross_return"]) > Decimal(r["cost_fraction"])
                          for r in training), 0) / Decimal(len(training))
        predictions = []
        for row in holdout:
            probability = _numeric_probability(model, row["values"])
            net = Decimal(row["gross_return"]) - Decimal(row["cost_fraction"])
            predictions.append({"row_id": row["row_id"], "subject": row["subject"],
                "decision_at": row["decision_at"], "resolve_after": row["resolve_after"],
                "feature_sha256": _digest(canonical({k: row[k] for k in
                    ("subject", "decision_at", "observed_at", "available_at", "source_ids", "values")})),
                "probability_positive_after_cost": str(probability),
                "positive_after_cost": net > 0, "recorded_after_cost_return": str(net)})
        targets = [Decimal(int(r["positive_after_cost"])) for r in predictions]
        candidate = _metrics([Decimal(r["probability_positive_after_cost"]) for r in predictions], targets)
        baselines = {"constant_half": _metrics([Decimal("0.5")] * len(targets), targets),
                     "training_prevalence": _metrics([prevalence] * len(targets), targets)}
        comparisons = {name: {
            "brier_improvement": str(Decimal(metrics["brier_score"]) - Decimal(candidate["brier_score"])),
            "log_loss_improvement": str(Decimal(metrics["log_loss"]) - Decimal(candidate["log_loss"])),
        } for name, metrics in baselines.items()}
    completed = _instant(clock())
    _check(completed >= fitted.bundle.training_run.trained_at >= started, "clock_reversed")
    report = {
        "schema": SCHEMA, "mode": "RETROSPECTIVE_HOLDOUT", "trading_authorized": False,
        "promotion_authorized": False, "forward_paper_evaluated": False,
        "out_of_sample_evaluated": True, "calibration_verified": False,
        "source_authenticity_verified": False, "started_at": started.isoformat(),
        "completed_at": completed.isoformat(), "package_sha256": package_sha,
        "evaluation_code_sha256": _digest(Path(__file__).read_bytes()),
        "input_rows": len(data["rows"]), "split": split,
        "training": fitted.diagnostics, "model_bundle": fitted.bundle.payload(),
        "candidate": candidate, "baselines": baselines, "comparisons": comparisons,
        "predictions": predictions,
        "limitations": [
            "Retrospective predictions are computed now; this is not a record of forecasts made then.",
            "Row and feature selection, source rights and availability times are supplied, not independently authenticated.",
            "A single chronological holdout does not establish repeatable skill or correct for repeated research trials.",
            "Calibration metrics describe this sample; they do not certify calibrated probabilities.",
            "Same-subject windows do not overlap; cross-subject and temporal dependence can remain.",
            "Recorded costs may omit real costs. Opportunity returns are not execution or portfolio P&L.",
        ],
    }
    report["sha256"] = _digest(canonical(report))
    return report
