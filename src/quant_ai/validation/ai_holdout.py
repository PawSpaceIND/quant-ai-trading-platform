"""Fail-closed evaluation contract for AI decisions on an untouched holdout.

This module evaluates recorded decisions only. It never calls a model, promotes a
strategy, or enables execution. The caller must freeze the decision stream before
the holdout starts and provide the provenance needed for independent review.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from quant_ai.validation.experiment import path_metrics

_REQUIRED_PROVENANCE = ("model", "model_version", "strategy_hash", "data_sha256", "decision_cutoff")


def evaluate_ai_holdout(
    prices: Sequence[float],
    positions: Sequence[int],
    *,
    holdout_start: int,
    cost_bps: float,
    provenance: Mapping[str, object],
) -> dict[str, object]:
    """Return a reproducible, after-cost report for recorded AI positions.

    A position at index ``t - 1`` earns the close-to-close return into ``t``.
    Decisions at or after ``holdout_start`` are rejected: the holdout must be
    frozen before evaluation so an operator cannot tune on the result.
    """
    if len(prices) != len(positions) or holdout_start < 1 or len(prices) - holdout_start < 20:
        raise ValueError("holdout must contain at least 20 aligned observations")
    if not math.isfinite(cost_bps) or not 0 <= cost_bps < 1000:
        raise ValueError("invalid turnover cost")
    if any(not math.isfinite(float(price)) or float(price) <= 0 for price in prices):
        raise ValueError("prices must be finite and positive")
    if any(position not in (0, 1) for position in positions):
        raise ValueError("AI positions must be binary long/cash decisions")
    missing = [key for key in _REQUIRED_PROVENANCE if not isinstance(provenance.get(key), str) or not str(provenance[key]).strip()]
    if missing:
        raise ValueError("missing holdout provenance: " + ", ".join(missing))
    if any(key not in _REQUIRED_PROVENANCE for key in provenance):
        raise ValueError("unknown holdout provenance field")

    returns: list[float] = []
    held = int(positions[holdout_start - 1])
    for t in range(holdout_start, len(prices)):
        desired = int(positions[t - 1])
        turnover = abs(desired - held)
        market_return = float(prices[t]) / float(prices[t - 1]) - 1
        returns.append(desired * market_return - turnover * cost_bps / 10000)
        held = desired
    if returns:
        returns[-1] -= held * cost_bps / 10000
    baseline = [float(prices[t]) / float(prices[t - 1]) - 1 for t in range(holdout_start, len(prices))]
    baseline[0] -= cost_bps / 10000
    baseline[-1] -= cost_bps / 10000
    return {
        "schema": "pramana.ai_holdout.v1",
        "scope": "recorded AI decisions on an untouched holdout; no promotion or execution permission",
        "holdout": {"range": [holdout_start, len(prices)], **path_metrics(returns)},
        "buy_and_hold": path_metrics(baseline),
        "cost_bps_per_turnover": cost_bps,
        "provenance": dict(provenance),
        "promotion_approved": False,
        "limitations": [
            "Recorded decisions do not prove provider authenticity or future profitability.",
            "Close-to-close returns do not model intrabar queue, latency or market impact.",
            "Independent review, forward paper evidence and calibration/drift analysis remain required.",
        ],
    }
