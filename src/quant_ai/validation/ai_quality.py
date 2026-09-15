"""Calibration and drift metrics for recorded AI probability evidence."""
from __future__ import annotations

import math
from collections.abc import Sequence


def assess_calibration_drift(probabilities: Sequence[float], outcomes: Sequence[int], *, window: int = 20) -> dict[str, object]:
    """Return bounded calibration/drift metrics; never makes a promotion decision."""
    if len(probabilities) != len(outcomes) or len(probabilities) < 2 * window or window < 10:
        raise ValueError("calibration requires two aligned windows of at least 10 observations")
    if any(not math.isfinite(float(p)) or not 0 <= float(p) <= 1 for p in probabilities):
        raise ValueError("probabilities must be finite values between zero and one")
    if any(outcome not in (0, 1) for outcome in outcomes):
        raise ValueError("outcomes must be binary")

    def metrics(ps: Sequence[float], ys: Sequence[int]) -> dict[str, float]:
        mean_probability = sum(float(p) for p in ps) / len(ps)
        mean_outcome = sum(ys) / len(ys)
        brier = sum((float(p) - y) ** 2 for p, y in zip(ps, ys)) / len(ps)
        return {"mean_probability": mean_probability, "event_rate": mean_outcome, "brier_score": brier, "calibration_error": mean_probability - mean_outcome}

    first = metrics(probabilities[:window], outcomes[:window])
    latest = metrics(probabilities[-window:], outcomes[-window:])
    return {
        "schema": "pramana.ai_quality.v1",
        "observations": len(probabilities),
        "window": window,
        "first": first,
        "latest": latest,
        "drift": {
            "mean_probability_delta": latest["mean_probability"] - first["mean_probability"],
            "event_rate_delta": latest["event_rate"] - first["event_rate"],
            "brier_delta": latest["brier_score"] - first["brier_score"],
        },
        "promotion_approved": False,
        "limitations": [
            "Window metrics are descriptive and do not establish causal model quality.",
            "Calibration requires a larger, independently reviewed sample and regime-aware analysis.",
            "This report does not enable live execution or replace forward paper evidence.",
        ],
    }
