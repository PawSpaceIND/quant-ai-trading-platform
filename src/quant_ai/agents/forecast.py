"""A directional forecast recorded before the outcome, so decisions can be scored.

Atlas has always recorded a stance and a confidence. Neither is a forecast: a confidence
is an agreement score, and nothing states what would have to happen for the decision to be
right, by when, or net of what cost. So the engine had opinions and no track record, and
the calibration scorer in ``quant_ai.research.calibration`` had nothing live to score -
its own input filter records exactly this case as ``probability_unavailable``.

This module turns the consensus the swarm already produces into one explicit claim:

    P(the forward return over the horizon is positive), for this instrument, by this time,
    net of this cost, invalidated by these conditions.

The mapping is deliberately simple and deliberately named. It reads the conviction-weighted
lean as a direction and the mean specialist confidence as how far from a coin flip to move,
so a book of all-neutral specialists forecasts exactly 0.5 and claims nothing. That mapping
is a hypothesis, not a calibration. Scoring it is the entire point: if 0.62 wins 62% of the
time it is sound, and if it does not, the Brier score against the baseline says so and the
mapping is refit under a NEW ``basis`` rather than changed in place, because a stored number
whose meaning silently moved is worse than no number.

Nothing here decides, sizes, or gates anything. A forecast is a record.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

SCHEMA = "pramana.directional_forecast.v1"
# The mapping that produced the probability. A refit ships a new id; the old rows keep
# their meaning and stay scoreable on their own terms.
BASIS = "weighted_lean_times_confidence.v1"
# Matches the horizon the outcome resolver already fills and decision quality already
# scores (``forward_return_60m``), so a forecast can be scored the day it resolves.
HORIZON_SECONDS = 3600
# The conviction-weighted lean is a mean of STANCE_SCORE, so a unanimous STRONG book is 2.
LEAN_SCALE = Decimal(2)
# Round-trip cost the outcome must clear before a directional call counts as right. Stored
# with the forecast so a later change of cost policy cannot silently rescore old rows.
COST_BPS = Decimal(10)
# A forecast of 0 or 1 asserts certainty and makes a log score infinite. Neither is honest.
FLOOR = Decimal("0.05")
CEILING = Decimal("0.95")
PLACES = Decimal("0.0001")


def probability_up(weighted_score: Decimal, confidence: Decimal) -> Decimal:
    """P(forward return positive), from the lean's direction and the book's conviction.

    All-neutral specialists give a lean of zero and therefore exactly 0.5: no claim. A
    unanimous, fully confident book is clamped short of certainty rather than allowed to
    assert it.
    """
    lean = max(Decimal(-1), min(Decimal(1), Decimal(weighted_score) / LEAN_SCALE))
    conviction = max(Decimal(0), min(Decimal(1), Decimal(confidence)))
    raw = Decimal("0.5") + Decimal("0.5") * lean * conviction
    return max(FLOOR, min(CEILING, raw)).quantize(PLACES)


def record(
    *,
    subject: str,
    weighted_score: Decimal,
    confidence: Decimal,
    now: datetime,
    reference_price: Decimal | None = None,
    stop_price: Decimal | None = None,
    take_profit_price: Decimal | None = None,
    horizon_seconds: int = HORIZON_SECONDS,
) -> dict[str, Any]:
    """The immutable forecast block carried in a decision's provenance."""
    if horizon_seconds <= 0:
        raise ValueError("forecast horizon must be positive")
    probability = probability_up(weighted_score, confidence)
    invalidation = [f"horizon_elapsed_at:{(now + timedelta(seconds=horizon_seconds)).isoformat()}"]
    if stop_price is not None:
        invalidation.append(f"stop_price_touched:{stop_price}")
    if take_profit_price is not None:
        invalidation.append(f"take_profit_touched:{take_profit_price}")
    return {
        "schema": SCHEMA,
        "basis": BASIS,
        "subject": subject,
        # Probability that the forward return over the horizon is positive. Direction is
        # the side of 0.5 this falls on; 0.5 exactly is an explicit absence of a claim.
        "probability_up": str(probability),
        "horizon_seconds": horizon_seconds,
        "made_at": now.isoformat(),
        "resolves_at": (now + timedelta(seconds=horizon_seconds)).isoformat(),
        "outcome_definition": "forward_return_net_of_cost_positive",
        "cost_bps": str(COST_BPS),
        "reference_price": str(reference_price) if reference_price is not None else None,
        "invalidation": invalidation,
    }


def probability_of(provenance: Any) -> Decimal | None:
    """The recorded probability, or None when the decision made no forecast."""
    block = provenance.get("forecast") if isinstance(provenance, dict) else None
    if not isinstance(block, dict) or block.get("schema") != SCHEMA:
        return None
    try:
        value = Decimal(str(block.get("probability_up")))
    except (ArithmeticError, TypeError, ValueError):
        return None
    return value if FLOOR <= value <= CEILING else None
