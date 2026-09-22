"""What the book expected, what happened, and who was arguing at the time.

Two reports, both read-only. Neither reweights a specialist, moves a floor or sizes a
position; the brief that asked for them is explicit that nothing reweights mid-session,
and nothing here could - they take journal rows and return numbers.

**The playbook scorecard** puts the expected value a decision recorded beside the forward
return it actually got, grouped by the regime it was judged under. The gap between those
two columns is the point. A regime whose decisions expect -0.0015 and realise +0.004 is
not being described by its payoffs, and since break-even probability is
``(e_risk + cost) / (e_win + e_risk)`` - a property of the declared numbers alone - that
gap is the evidence for changing them. Which is the one thing that must not be done by
taste.

**The roster scorecard** answers a question the hit rate cannot. On 22 September 2026 the
swarm produced 444 decisions and 440 holds, and the one name that moved - INDIGO, +1.77%
at 14:10 IST - was held with ``geopolitical-analyst`` voting BUY, ``indian-equities``
voting SELL and six specialists neutral. That is not a conviction floor refusing a weak
lean. It is two voters cancelling and six abstaining, and no per-specialist accuracy
number can see it, because accuracy scores the votes that were cast and the story here is
the votes that were not.

So this counts the shape of the roster on every decision - unanimous, split, or silent -
and reports what the market did next in each case. A book that is mostly silent and a
book that is mostly deadlocked need different fixes, and they look identical from the
outside: both hold.
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from decimal import Decimal
from statistics import median
from typing import Any

from quant_ai.analytics.decision_journal import parse_decimal
from quant_ai.analytics.decision_quality import HORIZON_COLUMN, direction, mean
from quant_ai.analytics.promotion import row_expected_value

SCHEMA = "pramana.scorecards.v1"
# Below this a group's numbers describe which decisions happened to resolve rather than
# the regime. Reported anyway, flagged, and never quietly folded into a conclusion.
MINIMUM_GROUP = 20
UNANIMOUS, SPLIT, SILENT = "unanimous", "split", "silent"


def _roster(row: Any) -> dict[str, dict[str, str]]:
    """The specialist matrix a row carries, or nothing if it cannot be read."""
    raw = row.get("agents")
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def roster_shape(row: Any) -> tuple[str, int, int, int]:
    """How the specialists stood: the shape, and the three counts behind it.

    ``split`` means at least one voter on each side. It is a different fact from a weak
    lean and from an empty room, and a hold that cannot tell the three apart is a hold
    nobody can act on.
    """
    votes = [direction(item.get("stance")) for item in _roster(row).values()
             if isinstance(item, dict)]
    buys = sum(1 for vote in votes if vote > 0)
    sells = sum(1 for vote in votes if vote < 0)
    flat = len(votes) - buys - sells
    if buys and sells:
        return SPLIT, buys, sells, flat
    if buys or sells:
        return UNANIMOUS, buys, sells, flat
    return SILENT, buys, sells, flat


def _summary(values: Sequence[Decimal]) -> dict[str, Any]:
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "mean": str(mean(list(ordered))) if ordered else None,
        "median": str(median(ordered)) if ordered else None,
    }


def playbook_scorecard(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Expected value against realised return, by the regime the decision ran under.

    Grouped by ``(regime, playbook)`` because a playbook can be selected by more than one
    regime label and the pair is what actually decided the floor and the size.
    """
    groups: dict[tuple[str, str], dict[str, list]] = {}
    for row in rows:
        key = (str(row.get("regime") or "unread"), str(row.get("playbook") or "none"))
        bucket = groups.setdefault(key, {"expected": [], "realised": [], "drift": [], "rows": []})
        bucket["rows"].append(row)
        value = row_expected_value(row)
        if value is not None:
            bucket["expected"].append(value)
        realised = parse_decimal(row.get(HORIZON_COLUMN))
        if realised is not None:
            bucket["realised"].append(realised)
        drift = parse_decimal(row.get("drift_bps"))
        if drift is not None:
            bucket["drift"].append(abs(drift))

    entries = []
    for (regime, playbook), bucket in sorted(groups.items()):
        expected, realised = bucket["expected"], bucket["realised"]
        # Only over rows that carry BOTH, so the gap is a comparison and not a difference
        # between two different samples that happen to share a regime.
        paired = [(row_expected_value(row), parse_decimal(row.get(HORIZON_COLUMN)))
                  for row in bucket["rows"]]
        paired = [(e, r) for e, r in paired if e is not None and r is not None]
        entries.append({
            "regime": regime,
            "playbook": playbook,
            "decisions": len(bucket["rows"]),
            "holds": sum(1 for row in bucket["rows"] if direction(row.get("stance")) == 0),
            "fills": sum(1 for row in bucket["rows"] if row.get("governance") == "filled"),
            "expected_value": _summary(expected),
            "realised_return": _summary(realised),
            # The number this report exists for. Positive means the book did better than
            # its own payoffs said it would, which is a statement about the payoffs.
            "realised_minus_expected": (
                str(mean([r - e for e, r in paired])) if paired else None
            ),
            "paired_rows": len(paired),
            "median_abs_drift_bps": str(median(bucket["drift"])) if bucket["drift"] else None,
            "thin": len(bucket["rows"]) < MINIMUM_GROUP,
        })
    return {
        "schema": SCHEMA,
        "report": "playbook",
        "minimum_group": MINIMUM_GROUP,
        "groups": entries,
        "limitations": [
            "A gap between expected and realised is evidence about the declared payoffs, not a signal.",
            "Realised return is the resolver's 60-minute forward move, gross of the fill's own friction.",
            "Groups below the minimum are reported with thin=true and conclude nothing.",
            "Nothing here reweights a specialist, moves a floor or sizes a position.",
        ],
    }


def roster_scorecard(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Whether the book was silent, deadlocked or agreeing - and what happened next.

    A split and a silence both produce a hold, and the two want opposite fixes: one is a
    roster that disagrees, the other a roster that is not there. Reported with the forward
    return in each bucket, so the cost of each shape is visible rather than assumed.
    """
    shapes: dict[str, dict[str, Any]] = {
        name: {"decisions": 0, "returns": [], "abstentions": []}
        for name in (UNANIMOUS, SPLIT, SILENT)
    }
    dissent: dict[str, dict[str, int]] = {}
    for row in rows:
        shape, buys, sells, flat = roster_shape(row)
        bucket = shapes[shape]
        bucket["decisions"] += 1
        bucket["abstentions"].append(Decimal(flat))
        realised = parse_decimal(row.get(HORIZON_COLUMN))
        if realised is not None:
            bucket["returns"].append(realised)
        if shape != SPLIT:
            continue
        # A lone dissenter on a split decision: the specialist the rest of the room
        # disagreed with. Scored on whether the market went its way, which is the only
        # thing that makes a dissent worth keeping or worth discounting.
        for agent, item in _roster(row).items():
            if not isinstance(item, dict):
                continue
            vote = direction(item.get("stance"))
            if vote == 0 or realised is None:
                continue
            minority = (vote > 0 and buys == 1) or (vote < 0 and sells == 1)
            if not minority:
                continue
            tally = dissent.setdefault(agent, {"dissents": 0, "vindicated": 0})
            tally["dissents"] += 1
            if (vote > 0 and realised > 0) or (vote < 0 and realised < 0):
                tally["vindicated"] += 1

    return {
        "schema": SCHEMA,
        "report": "roster",
        "shapes": {
            name: {
                "decisions": bucket["decisions"],
                "mean_abstaining_specialists": (
                    str(mean(bucket["abstentions"])) if bucket["abstentions"] else None
                ),
                "forward_return": _summary(bucket["returns"]),
            }
            for name, bucket in shapes.items()
        },
        # Sorted by name so two readers of one journal never disagree about the order.
        "lone_dissent": {agent: dict(tally) for agent, tally in sorted(dissent.items())},
        "limitations": [
            "A split is at least one voter on each side, not a weak lean.",
            "A silent book is every voter neutral or abstaining; it is not a disagreement.",
            "Vindication counts direction only, gross of cost, and is not a fill or a P&L.",
            "Nothing here reweights a specialist, moves a floor or sizes a position.",
        ],
    }
