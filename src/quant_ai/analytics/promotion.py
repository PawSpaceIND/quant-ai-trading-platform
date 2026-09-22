"""Whether the forecast has earned the right to gate a trade, and why not yet.

The EV gate is off, and arming it is an operator act that needs evidence rather than
confidence. This is that evidence: one deterministic function over recorded journal rows
that answers a single question - has this basis produced enough resolved forecasts, better
than a coin, with filled decisions that actually made money net of their stored cost?

It fails closed. An input that is missing is named and the verdict is refusal, never a
pass computed over the fields that happened to be present. A report that quietly omits the
drift distribution or the drawdown and returns "pass" is worse than no report, because it
would be read as the thing that checked them.

Two inputs are missing by construction today and the report says so on every run: the
signed entry drift on fills, and the book's realised drawdown. Neither is journaled yet.
That is not a defect here - it is the reason the gate cannot be armed, stated as a fact
rather than discovered later.

Nothing in this module decides, sizes or gates. It reports.
"""
from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from statistics import median
from typing import Any

from quant_ai.agents.expected_value import expected_value
from quant_ai.analytics.decision_journal import parse_decimal
from quant_ai.analytics.forecast_scoring import summarize

SCHEMA = "pramana.promotion_report.v1"
# Below this the numbers describe which decisions happened to resolve, not the mapping.
MINIMUM_RESOLVED = 200
# The coin the brief promotes against. The base rate is the harder test and is reported
# beside it, because a forecaster that has only learned how often the move happens beats
# the coin while knowing nothing about any particular decision.
COIN = Decimal("0.5")
# A book whose probabilities sit mid-range while almost every expected value is negative
# is not being told "do not trade" by its forecast; it is being told that by its payoffs.
PLACEHOLDER_NEGATIVE_SHARE = Decimal("0.90")
PLACEHOLDER_PROBABILITY_BAND = (Decimal("0.45"), Decimal("0.75"))
FILLED = "filled"


def row_expected_value(row: dict[str, Any]) -> Decimal | None:
    """EV from the row's OWN recorded inputs, never from a current setting.

    This is not a recomputed feature. Probability, cost, expected return and expected risk
    were each written with the decision; combining them is what a scorer does, and using
    today's cost policy instead would rescore a decision made under yesterday's.
    """
    probability = parse_decimal(row.get("forecast_probability_up"))
    cost_bps = parse_decimal(row.get("forecast_cost_bps"))
    win = parse_decimal(row.get("expected_return"))
    risk = parse_decimal(row.get("expected_risk"))
    if probability is None or cost_bps is None or win is None or risk is None:
        return None
    return expected_value(probability, expected_return=win, expected_risk=risk, cost_bps=cost_bps)


def _filled_buys(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Decisions that became positions. Probes count; a refusal is not a loss."""
    return [
        row for row in rows
        if row.get("governance") == FILLED and str(row.get("side") or "").upper() == "BUY"
    ]


def _payoff_note(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Whether the expected values are being decided by the probability or by the payoffs.

    A break-even probability of (risk + cost) / (win + risk) is a property of the declared
    payoffs alone. When those payoffs put it beyond what the mapping can reach, every EV
    is negative whatever the forecast says, and arming a gate on it would silence the book
    for a reason that has nothing to do with calibration.
    """
    values, probabilities = [], []
    for row in rows:
        value = row_expected_value(row)
        probability = parse_decimal(row.get("forecast_probability_up"))
        if value is not None and probability is not None:
            values.append(value)
            probabilities.append(probability)
    if not values:
        return {"assessed": False, "reason": "no_row_carried_both_a_probability_and_a_payoff"}
    negative = Decimal(sum(1 for value in values if value < 0)) / Decimal(len(values))
    middling = median(probabilities)
    low, high = PLACEHOLDER_PROBABILITY_BAND
    return {
        "assessed": True,
        "rows": len(values),
        "negative_expected_value_share": str(negative),
        "median_probability_up": str(middling),
        # Not a verdict on the forecast. A verdict on the numbers beside it.
        "payoffs_look_declared_not_measured": bool(
            negative >= PLACEHOLDER_NEGATIVE_SHARE and low <= middling <= high
        ),
    }


def promotion_report(
    rows: Sequence[dict[str, Any]],
    *,
    entry_drift_bps: Sequence[Any] | None = None,
    realised_max_drawdown: Any | None = None,
    policy_max_drawdown: Any | None = None,
) -> dict[str, Any]:
    """Deterministic over its inputs: the same rows always give the same report.

    ``entry_drift_bps`` overrides the journal's own ``drift_bps`` column, for a replay
    scoring a window from outside the ledger; absent, the filled rows answer for
    themselves. ``realised_max_drawdown`` is still the caller's, and absent it is named as
    missing and the verdict refuses.
    """
    scoring = summarize(list(rows))
    bases = scoring["by_basis"]
    # The champion is the basis with the most scored rows; ties break by name so two
    # readers of the same journal never disagree about which mapping was assessed.
    champion = max(bases, key=lambda item: (item["scored"], item["basis"]), default=None)
    resolved = champion["scored"] if champion else 0
    brier = parse_decimal(champion["brier_score"]) if champion else None
    coin = parse_decimal(champion["baselines"]["coin_flip"]["brier_score"]) if champion and champion["baselines"] else None

    filled = _filled_buys(rows)
    realised = [parse_decimal(row.get("realized_net_pnl")) for row in filled]
    settled = [value for value in realised if value is not None]
    mean_net = (sum(settled, Decimal(0)) / Decimal(len(settled))) if settled else None

    # The journal now carries the gate's own measurement per decision, so the caller no
    # longer has to supply it. The parameter stays as an override for a replay scoring a
    # window from outside the ledger; absent, the rows answer for themselves. Measured on
    # fills, because the question is the drift the book actually paid, not the drift of
    # everything the gate looked at.
    supplied = [parse_decimal(value) for value in (entry_drift_bps or [])]
    journaled = [parse_decimal(row.get("drift_bps")) for row in filled]
    drifts = supplied if entry_drift_bps is not None else journaled
    drift_values = [abs(value) for value in drifts if value is not None]
    median_drift = median(drift_values) if drift_values else None

    drawdown = parse_decimal(realised_max_drawdown)
    policy = parse_decimal(policy_max_drawdown)

    missing = []
    if champion is None:
        missing.append("no_scored_basis")
    if brier is None or coin is None:
        missing.append("brier_or_baseline_unavailable")
    if mean_net is None:
        missing.append("no_settled_filled_buy")
    if median_drift is None:
        missing.append("entry_drift_not_journaled")
    if drawdown is None or policy is None:
        missing.append("drawdown_or_policy_unavailable")

    insufficient = resolved < MINIMUM_RESOLVED
    beats_coin = brier is not None and coin is not None and brier < coin
    profitable = mean_net is not None and mean_net > 0
    within_drawdown = drawdown is not None and policy is not None and drawdown <= policy
    verdict = (
        "insufficient_sample" if insufficient
        else "missing_inputs" if missing
        else "pass" if beats_coin and profitable and within_drawdown
        else "fail"
    )
    return {
        "schema": SCHEMA,
        "verdict": verdict,
        # A pass is the only value that may arm the gate, and it is never inferred from
        # the fields below: a reader that ignores `verdict` and adds up the metrics itself
        # would promote on a partial report.
        "promotion_authorized": verdict == "pass",
        "minimum_resolved": MINIMUM_RESOLVED,
        "resolved_forecast_count": resolved,
        "basis": champion["basis"] if champion else None,
        "brier_score": champion["brier_score"] if champion else None,
        "brier_baseline_coin_flip": (champion["baselines"]["coin_flip"]["brier_score"]
                                     if champion and champion["baselines"] else None),
        # Reported, never gated on, and the harder of the two: a forecaster that has only
        # learned the base rate beats the coin and knows nothing about any decision.
        "skill_vs_base_rate": champion["skill_vs_base_rate"] if champion else None,
        "reliability": champion["reliability"] if champion else [],
        "mean_net_ev_filled_buys": str(mean_net) if mean_net is not None else None,
        "filled_buys": len(filled),
        "settled_filled_buys": len(settled),
        "median_abs_entry_drift_bps": str(median_drift) if median_drift is not None else None,
        "realised_max_drawdown": str(drawdown) if drawdown is not None else None,
        "policy_max_drawdown": str(policy) if policy is not None else None,
        "payoffs": _payoff_note(rows),
        "missing_inputs": missing,
        "limitations": [
            "A pass is evidence that a mapping was worth scoring, not that an edge exists.",
            "Brier against a coin is the weaker test; skill against the base rate is reported beside it.",
            "Filled-buy P&L is paper, net of recorded fees only; unrecorded costs remain outside it.",
            "Refused decisions score the forecast directionally and are never fill P&L.",
            "Entry drift is the gate's own measurement on filled rows; realised drawdown is still the caller's.",
        ],
    }
