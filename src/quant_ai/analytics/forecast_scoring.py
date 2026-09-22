"""Scoring for the directional forecasts the engine records on every decision.

The recording half writes, on each decision, the probability that the forward return over
a horizon clears a stated cost. This is the half that asks whether those numbers were any
good, which is the only thing that can turn a stream of opinions into a track record.

Three rules shape everything here, and each exists because the obvious alternative
flatters the system:

* **Never pool across bases.** A refit ships a new ``forecast_basis``; the numbers written
  under the old mapping mean something different. Pooling them produces one curve that
  describes no mapping that ever ran.
* **Score against the base rate, not a coin.** A forecast that has merely learned how
  often the market goes up beats 0.5 on every metric and knows nothing about any
  particular decision. The base-rate baseline is the one that has to be beaten; the coin
  is reported too, because it is the weaker claim and saying both makes the gap visible.
* **Use each row's own stored cost and horizon.** The outcome a forecast claimed is the
  one written with it. Rescoring old rows under today's cost policy, or against a horizon
  they never referred to, is the same error as pooling bases.

Brier and log score are both proper: they are minimised by stating your true belief, so
neither can be improved by hedging towards 0.5 or by exaggerating. Lower is better for
both. No metric here is an edge claim; see ``quant_ai.analytics.decision_quality`` for
what the surrounding evidence can and cannot establish.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

from quant_ai.agents.forecast import CEILING, FLOOR
from quant_ai.analytics.decision_journal import parse_decimal

SCHEMA = "pramana.forecast_scoring.v1"
# The horizon column each forecast horizon resolves against. A basis that forecasts a
# horizon with no resolver column is not scoreable and is reported as such, never scored
# against a different horizon's outcome.
HORIZON_COLUMNS = {3600: "forward_return_60m"}
# Below this, the numbers are printed and no skill is claimed from them. A Brier score on
# a handful of decisions is dominated by which decisions happened to resolve.
MINIMUM_SCORED = 30
# Reliability bins, matching the calibration strip the dashboard already draws.
BINS = 10
BASIS_POINT = Decimal(10000)


def number(value: Decimal | None) -> float | None:
    """Render a Decimal for JSON: six places, null when undefined."""
    return None if value is None else round(float(str(value)), 6)


def _mean(values: list[Decimal]) -> Decimal | None:
    return sum(values, Decimal(0)) / Decimal(len(values)) if values else None


def outcome_of(row: dict[str, Any]) -> tuple[Decimal, int] | None:
    """(probability, outcome) for one row, or None when the row cannot be scored.

    The outcome is the event the forecast actually named: the forward return over *its*
    horizon, net of *its* stored cost, being positive. A row is skipped when the forecast
    is absent or out of range, when its horizon has no resolver column, or when the
    outcome has not resolved yet - never scored against a substitute.
    """
    probability = parse_decimal(row.get("forecast_probability_up"))
    if probability is None or not (FLOOR <= probability <= CEILING):
        return None
    horizon = row.get("forecast_horizon_seconds")
    try:
        column = HORIZON_COLUMNS[int(horizon)]
    except (KeyError, TypeError, ValueError):
        return None
    forward = parse_decimal(row.get(column))
    if forward is None:
        return None
    cost = parse_decimal(row.get("forecast_cost_bps"))
    if cost is None or cost < 0:
        return None
    return probability, int(forward - cost / BASIS_POINT > 0)


def _log_score(probability: Decimal, outcome: int) -> Decimal:
    """Negative log likelihood of the observed outcome. Finite because the mapping clamps."""
    stated = probability if outcome else Decimal(1) - probability
    return -stated.ln()


def _reliability(scored: list[tuple[Decimal, int]]) -> list[dict[str, Any]]:
    """Forecast probability against observed frequency, in fixed bins."""
    groups: dict[int, list[tuple[Decimal, int]]] = defaultdict(list)
    for probability, outcome in scored:
        index = min(BINS - 1, int(probability * BINS))
        groups[index].append((probability, outcome))
    rows = []
    for index in range(BINS):
        members = groups.get(index, [])
        rows.append(
            {
                "lower": number(Decimal(index) / BINS),
                "upper": number(Decimal(index + 1) / BINS),
                "forecasts": len(members),
                "mean_forecast": number(_mean([p for p, _ in members])),
                "observed_frequency": number(_mean([Decimal(o) for _, o in members])),
            }
        )
    return rows


def _decomposition(scored: list[tuple[Decimal, int]], base_rate: Decimal, brier: Decimal) -> dict[str, Any]:
    """Murphy's decomposition of the Brier score, plus the part the bins cannot explain.

    Reliability is how far each bin's mean forecast sits from what that bin actually did,
    so lower is better and zero is perfect calibration. Resolution is how far the bins sit
    from the base rate, so higher is better and zero means the forecasts never separated
    anything. Uncertainty is the variance of the outcome itself, which no forecaster can
    change. A single Brier score hides all three.

    The familiar identity ``Brier = reliability - resolution + uncertainty`` is exact only
    when every forecast inside a bin is the same number. Forecasts here are continuous, so
    binning them loses the spread within each bin and the three terms fall short of the
    score they claim to explain. That shortfall is reported as ``within_bin`` rather than
    left as a silent discrepancy: the four terms always reconstruct the Brier score
    exactly, and a large ``within_bin`` means these bins are too coarse for these
    forecasts, which is worth knowing about the view rather than hiding.
    """
    groups: dict[int, list[tuple[Decimal, int]]] = defaultdict(list)
    for probability, outcome in scored:
        groups[min(BINS - 1, int(probability * BINS))].append((probability, outcome))
    total = Decimal(len(scored))
    reliability = Decimal(0)
    resolution = Decimal(0)
    for members in groups.values():
        weight = Decimal(len(members)) / total
        mean_forecast = _mean([p for p, _ in members]) or Decimal(0)
        observed = _mean([Decimal(o) for _, o in members]) or Decimal(0)
        reliability += weight * (mean_forecast - observed) ** 2
        resolution += weight * (observed - base_rate) ** 2
    uncertainty = base_rate * (Decimal(1) - base_rate)
    # Reconcile the numbers as published, not as computed. Every field here is rounded to
    # six places, and four rounded terms do not re-add to a rounded total, so a reader
    # checking the arithmetic on the page would find it short by up to a couple of units
    # in the last place. The residual is therefore taken between the published figures:
    # it carries that rounding dust along with the within-bin spread, which is far smaller
    # than the quantity itself and buys a table that adds up exactly as shown.
    shown = {
        "reliability": number(reliability),
        "resolution": number(resolution),
        "uncertainty": number(uncertainty),
    }
    residual = number(brier) - (shown["reliability"] - shown["resolution"] + shown["uncertainty"])
    return {**shown, "within_bin": round(residual, 6)}


def score_basis(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Every metric for one basis' scored rows. ``rows`` must share a ``forecast_basis``."""
    scored = [outcome for row in rows if (outcome := outcome_of(row)) is not None]
    if not scored:
        return {
            "scored": 0,
            "insufficient_sample": True,
            "base_rate": None,
            "brier_score": None,
            "log_score": None,
            "baselines": None,
            "skill_vs_base_rate": None,
            "skill_vs_coin_flip": None,
            "decomposition": None,
            "reliability": _reliability([]),
        }
    total = Decimal(len(scored))
    base_rate = sum((Decimal(o) for _, o in scored), Decimal(0)) / total
    brier = sum(((p - o) ** 2 for p, o in scored), Decimal(0)) / total
    log_score = sum((_log_score(p, o) for p, o in scored), Decimal(0)) / total

    coin = Decimal("0.5")
    brier_coin = sum(((coin - o) ** 2 for _, o in scored), Decimal(0)) / total
    log_coin = -coin.ln()
    # The base-rate baseline knows the unconditional frequency and nothing else. Clamped
    # the same way a forecast is, so a sample with no losses cannot make its log score
    # infinite and win by default.
    flat = max(FLOOR, min(CEILING, base_rate))
    brier_base = sum(((flat - o) ** 2 for _, o in scored), Decimal(0)) / total
    log_base = sum((_log_score(flat, o) for _, o in scored), Decimal(0)) / total

    def skill(reference: Decimal) -> Decimal | None:
        return None if reference <= 0 else (reference - brier) / reference

    return {
        "scored": len(scored),
        "insufficient_sample": len(scored) < MINIMUM_SCORED,
        "base_rate": number(base_rate),
        "brier_score": number(brier),
        "log_score": number(log_score),
        "baselines": {
            "coin_flip": {"brier_score": number(brier_coin), "log_score": number(log_coin)},
            "base_rate": {"probability": number(flat), "brier_score": number(brier_base), "log_score": number(log_base)},
        },
        # 1 means perfect, 0 means no better than the baseline, negative means worse.
        "skill_vs_base_rate": number(skill(brier_base)),
        "skill_vs_coin_flip": number(skill(brier_coin)),
        "decomposition": _decomposition(scored, base_rate, brier),
        "reliability": _reliability(scored),
    }


def unscoreable(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Why rows with a forecast could not be scored. Silence about these would flatter the rest."""
    reasons = {"no_forecast": 0, "unknown_horizon": 0, "outcome_unresolved": 0, "invalid_probability": 0}
    for row in rows:
        raw = row.get("forecast_probability_up")
        if raw is None:
            reasons["no_forecast"] += 1
            continue
        probability = parse_decimal(raw)
        if probability is None or not (FLOOR <= probability <= CEILING):
            reasons["invalid_probability"] += 1
            continue
        try:
            column = HORIZON_COLUMNS[int(row.get("forecast_horizon_seconds"))]
        except (KeyError, TypeError, ValueError):
            reasons["unknown_horizon"] += 1
            continue
        if parse_decimal(row.get(column)) is None or parse_decimal(row.get("forecast_cost_bps")) is None:
            reasons["outcome_unresolved"] += 1
    return reasons


def verdict(basis: str, result: dict[str, Any]) -> str:
    """One sentence stating what this basis has and has not established."""
    scored, skill = result["scored"], result["skill_vs_base_rate"]
    if result["insufficient_sample"]:
        return (
            f"{basis}: {scored} scored forecasts, fewer than the {MINIMUM_SCORED} this report "
            "applies any skill claim to. The numbers below describe this sample only."
        )
    if skill is None:
        return f"{basis}: {scored} scored forecasts, but the base rate leaves no room to improve on, so no skill can be measured."
    if skill <= 0:
        return (
            f"{basis}: {scored} scored forecasts with skill {skill:+.3f} against the base rate. "
            "The forecasts are no better than always stating how often the move happens, so they "
            "carry no information about any particular decision."
        )
    return (
        f"{basis}: {scored} scored forecasts with skill {skill:+.3f} against the base rate "
        f"(Brier {result['brier_score']:.4f} versus {result['baselines']['base_rate']['brier_score']:.4f}). "
        "This is paper evidence over one window and is not a live-trading approval."
    )


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Forecast scoring for a set of journal rows, split by the basis each was written under."""
    by_basis: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        basis = row.get("forecast_basis")
        # A basis is only listed when something was actually forecast under it. A row that
        # names a mapping and states no probability was not a forecast that mapping made.
        if isinstance(basis, str) and basis and row.get("forecast_probability_up") is not None:
            by_basis[basis].append(row)
    bases = []
    for basis in sorted(by_basis):
        result = score_basis(by_basis[basis])
        bases.append({"basis": basis, **result, "verdict": verdict(basis, result)})
    # Most-scored first, so the mapping with the most evidence reads first.
    bases.sort(key=lambda item: (-item["scored"], item["basis"]))
    return {
        "schema": SCHEMA,
        "minimum_scored": MINIMUM_SCORED,
        "decisions": len(rows),
        "with_forecast": sum(1 for row in rows if row.get("forecast_probability_up") is not None),
        "unscoreable": unscoreable(rows),
        "by_basis": bases,
        "limitations": [
            (
                "Brier and log score are proper scoring rules: lower is better and neither can "
                "be improved by hedging towards 0.5 or by exaggerating confidence."
            ),
            (
                "Skill is measured against the base rate of this sample, not against a coin. A "
                "forecast that has only learned how often the move happens scores zero skill "
                "here while beating a coin flip, which is why both baselines are shown."
            ),
            (
                "Bases are never pooled. A refit ships a new basis and its rows are scored on "
                "their own terms; rows written under a mapping that no longer runs are still "
                "shown."
            ),
            (
                "Each row is scored against the cost and horizon stored with it, so a later "
                "change of cost policy cannot rescore decisions made under the old one."
            ),
            (
                "Forecasts whose outcome has not resolved are excluded, not counted as misses. "
                "The unscoreable counts state how many and why."
            ),
            (
                "Decisions inside a session are serially correlated, so these are not "
                "independent observations and no confidence interval is implied."
            ),
        ],
    }
