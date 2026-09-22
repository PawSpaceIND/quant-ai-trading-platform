"""What a decision actually won and lost, as opposed to what its specialists declared.

Every specialist on this book declares the same payoff on every instrument in every
regime: a 1% win against a 2% risk. Those numbers describe no measurement. Their effect is
arithmetic and severe - break-even probability is

    (e_risk + cost) / (e_win + e_risk)

a property of the declared numbers alone, which at 1%, 2% and 10 basis points is 0.70.
The live swarm produces 0.59 to 0.65. So every entry the engine takes is negative expected
value at its own stated payoffs, and no improvement in the forecast can change that,
because the forecast is not what is wrong.

This module computes the other half from what the market actually did: over resolved
forward returns, net of the cost each row itself stored, the mean winning move and the
mean losing move, by symbol and by the playbook and regime the decision ran under.

Three deliberate choices.

**It is recorded, not applied.** The live path keeps the declared numbers. Swapping them
silently would rescore the meaning of every EV already written, and the point of measuring
is to find out whether the measurement is worth trusting - which a sample of eighteen rows
cannot yet answer.

**It uses the row's own cost, not a current setting.** The same rule ``outcome_of``
already applies when it decides whether a forecast was right: a later change of cost
policy must not restate what an old decision won.

**A refit ships a new id.** The artifact's id is a hash of its own content, so
``empirical:<id>`` names one measurement over one window. Nothing back-fills; rows written
under an earlier id keep meaning what they meant.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from statistics import median
from typing import Any

from quant_ai.agents.expected_value import DECLARED
from quant_ai.analytics.decision_journal import parse_decimal
from quant_ai.analytics.forecast_scoring import BASIS_POINT, HORIZON_COLUMNS

SCHEMA = "pramana.empirical_payoffs.v1"
# Matches forecast_scoring.MINIMUM_SCORED. Below it the mean describes which decisions
# happened to resolve rather than the instrument, and the group says so instead.
MINIMUM_SAMPLE = 30
PLACES = Decimal("0.000001")


def net_move(row: Any) -> Decimal | None:
    """The forward move over the forecast's own horizon, net of its own stored cost.

    Guarded exactly as ``forecast_scoring.outcome_of`` guards the same arithmetic - it
    keeps the sign of this number and throws the magnitude away, which is the half needed
    here. A row whose horizon has no resolver column, or which has not resolved, has no
    move; it is never scored against a substitute horizon.
    """
    if not isinstance(row, dict):
        return None
    try:
        column = HORIZON_COLUMNS[int(row.get("forecast_horizon_seconds"))]
    except (KeyError, TypeError, ValueError):
        return None
    forward = parse_decimal(row.get(column))
    cost = parse_decimal(row.get("forecast_cost_bps"))
    if forward is None or cost is None or cost < 0:
        return None
    return forward - cost / BASIS_POINT


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    return (sum(values, Decimal(0)) / Decimal(len(values))).quantize(PLACES)


def _window(rows: Sequence[dict[str, Any]]) -> dict[str, str | None]:
    stamps = sorted(str(row.get("decided_at")) for row in rows if row.get("decided_at"))
    return {"first_decided_at": stamps[0] if stamps else None,
            "last_decided_at": stamps[-1] if stamps else None}


def _group(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """One group's measured payoffs, or an honest statement that it has none."""
    moves = [(row, move) for row in rows if (move := net_move(row)) is not None]
    wins = [move for _, move in moves if move > 0]
    losses = [-move for _, move in moves if move < 0]
    # A move of exactly zero won nothing and lost nothing. Counting it either way would
    # move a mean it has no claim on.
    flat = len(moves) - len(wins) - len(losses)
    costs = [value for row, _ in moves if (value := parse_decimal(row.get("forecast_cost_bps"))) is not None]
    win_hat, loss_hat = _mean(wins), _mean(losses)
    return {
        "e_win_hat": str(win_hat) if win_hat is not None else None,
        "e_loss_hat": str(loss_hat) if loss_hat is not None else None,
        "n": len(moves),
        "wins": len(wins),
        "losses": len(losses),
        "flat": flat,
        "decisions": len(rows),
        # Derived, and the number the whole module exists to produce: what probability
        # these measured payoffs would actually require, against the declared 0.70.
        "break_even_probability": _break_even(win_hat, loss_hat, costs),
        "thin": len(moves) < MINIMUM_SAMPLE,
        **_window(rows),
    }


def _break_even(win: Decimal | None, loss: Decimal | None, costs: Sequence[Decimal]) -> str | None:
    """(e_loss + cost) / (e_win + e_loss), at the group's median stored cost."""
    if win is None or loss is None or not costs:
        return None
    denominator = win + loss
    if denominator <= 0:
        return None
    return str(((loss + median(costs) / BASIS_POINT) / denominator).quantize(Decimal("0.0001")))


def payoffs(rows: Sequence[dict[str, Any]], *, key) -> dict[str, dict[str, Any]]:
    """Measured payoffs grouped by ``key(row)``. Sorted, so two readers never disagree."""
    grouped: dict[str, list] = {}
    for row in rows:
        grouped.setdefault(str(key(row)), []).append(row)
    return {name: _group(members) for name, members in sorted(grouped.items())}


def _playbook_key(row: Any) -> str:
    return f"{row.get('playbook') or 'none'}:{row.get('regime') or 'unread'}"


def artifact(rows: Sequence[dict[str, Any]], *, generated_at: datetime | None = None) -> dict[str, Any]:
    """The measurement, with an id that is a hash of the measurement itself.

    ``generated_at`` is outside the hash: the same rows measured twice are the same
    artifact, and an id that moved with the clock would make ``empirical:<id>`` useless
    as a statement about which numbers a decision was scored under.
    """
    content = {
        "schema": SCHEMA,
        "minimum_sample": MINIMUM_SAMPLE,
        "by_symbol": payoffs(rows, key=lambda row: row.get("symbol") or "unknown"),
        "by_playbook": payoffs(rows, key=_playbook_key),
        "overall": _group(list(rows)),
    }
    digest = hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]
    return {
        **content,
        "id": digest,
        "generated_at": generated_at.isoformat() if generated_at is not None else None,
        "limitations": [
            "Measured from resolved forward returns, gross of the fill's own friction.",
            "A group below the minimum is reported with thin=true and concludes nothing.",
            "Recorded only. The live expected value keeps the declared specialist numbers.",
            "A refit ships a new id; rows written under an earlier id are never back-filled.",
        ],
    }


def source_label(found: Any) -> str:
    """``empirical:<id>`` for an artifact, or ``declared`` when there is none.

    An artifact without an id is not a source. Naming it one would let a decision claim a
    measurement that cannot be looked up.
    """
    identifier = found.get("id") if isinstance(found, dict) else None
    return f"empirical:{identifier}" if identifier else DECLARED


def group_for(found: Any, *, symbol: Any = None, playbook: Any = None, regime: Any = None) -> dict | None:
    """The measured payoffs that apply to one decision, symbol first, then playbook.

    None when nothing measured applies - including when the group exists but is thin.
    A thin group is a number without a claim behind it, and handing it to an expected
    value would dress eighteen rows as a measurement.
    """
    if not isinstance(found, dict):
        return None
    candidates = [
        (found.get("by_symbol") or {}).get(str(symbol)) if symbol is not None else None,
        (found.get("by_playbook") or {}).get(f"{playbook or 'none'}:{regime or 'unread'}"),
    ]
    for candidate in candidates:
        if (isinstance(candidate, dict) and not candidate.get("thin")
                and candidate.get("e_win_hat") and candidate.get("e_loss_hat")):
            return candidate
    return None
