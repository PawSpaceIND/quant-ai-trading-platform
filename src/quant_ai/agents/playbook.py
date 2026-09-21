"""Regime playbooks: what Atlas demands of a lean, and how much of it he takes, per regime.

The specialists are the strategy; the regime decides how they are held to account. A
playbook names, for one deterministic regime label (``quant_ai.intelligence.regime``),
the extra conviction the consensus floor demands there, the share of the plan-sized entry
Atlas may take, and whether the exploration budget may probe. Every adjustment tightens:
a playbook raises the floor and shrinks an entry, never the reverse, so the routing
switched off is always the looser setting. The playbook and the sizing it produced are
written into every decision's provenance, so decision quality can score each playbook on
its own instead of one blended number.

Conviction sizing sits beside the playbooks: a BUY at the floor takes half the plan-sized
quantity and a BUY at ``FULL_SIZE_CONFIDENCE`` takes all of it, linearly in between. The
plan's four caps (risk, position, trade, capital) are computed first and never exceeded.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, InvalidOperation

from quant_ai.intelligence.regime import (
    HIGH_VOLATILITY,
    INSUFFICIENT_HISTORY,
    RANGING,
    REGIME_LABELS,
    TRENDING_DOWN,
    TRENDING_UP,
)

UNKNOWN_REGIME = "unknown"
# Above this decision confidence an entry takes the full plan-sized quantity.
FULL_SIZE_CONFIDENCE = Decimal("0.85")
# At the consensus floor an entry takes this share of the plan-sized quantity.
FLOOR_SIZE_SHARE = Decimal("0.50")
PLACES = Decimal("0.0001")


def _fixed(value: Decimal) -> str:
    return str(value.quantize(PLACES))


@dataclass(frozen=True)
class RegimePlaybook:
    regime: str
    name: str
    # Added to the policy's consensus floor before the specialists' average is judged.
    floor_adjustment: Decimal
    # Share of the plan-sized (and conviction-sized) entry this regime allows.
    size_multiplier: Decimal
    # Whether the exploration budget may turn a hold into a probe in this regime.
    probes_allowed: bool
    note: str

    def __post_init__(self) -> None:
        if not Decimal(0) <= self.floor_adjustment <= Decimal("0.40"):
            raise ValueError("floor_adjustment must be in [0, 0.40]")
        if not Decimal(0) < self.size_multiplier <= Decimal(1):
            raise ValueError("size_multiplier must be in (0, 1]")
        if not self.name or any(c.isspace() or c in ";=:" for c in self.name):
            raise ValueError("playbook name must be a single token")

    def floor(self, base: Decimal) -> Decimal:
        return base + self.floor_adjustment

    def provenance(self, base_floor: Decimal) -> dict[str, object]:
        return {
            "name": self.name,
            "regime": self.regime,
            "floor": _fixed(self.floor(base_floor)),
            "floor_adjustment": _fixed(self.floor_adjustment),
            "size_multiplier": _fixed(self.size_multiplier),
            "probes_allowed": self.probes_allowed,
        }


PLAYBOOKS: dict[str, RegimePlaybook] = {
    TRENDING_UP: RegimePlaybook(
        TRENDING_UP, "trend_following", Decimal(0), Decimal("1.00"), True,
        "ride the trend: the plan floor, full plan size, probes allowed",
    ),
    RANGING: RegimePlaybook(
        RANGING, "range_trading", Decimal("0.05"), Decimal("0.75"), True,
        "a range gives back what it gives: a slightly higher floor at three-quarter size",
    ),
    TRENDING_DOWN: RegimePlaybook(
        TRENDING_DOWN, "defensive", Decimal("0.10"), Decimal("0.50"), False,
        "a long-only book in a falling tape buys only on strong consensus, at half size, and never probes",
    ),
    HIGH_VOLATILITY: RegimePlaybook(
        HIGH_VOLATILITY, "crisis_standdown", Decimal("0.15"), Decimal("0.35"), False,
        "stand down: only an exceptional consensus trades, at a third of size, and never probes",
    ),
    INSUFFICIENT_HISTORY: RegimePlaybook(
        INSUFFICIENT_HISTORY, "cautious_default", Decimal("0.05"), Decimal("0.50"), False,
        "no regime read yet: a higher floor at half size, no probes until the bars exist",
    ),
}
# A label the classifier does not produce, or no evidence context at all, is treated the
# way missing history is: cautiously.
DEFAULT_PLAYBOOK = RegimePlaybook(
    UNKNOWN_REGIME, "cautious_default", Decimal("0.05"), Decimal("0.50"), False,
    PLAYBOOKS[INSUFFICIENT_HISTORY].note,
)
# The routing switched off: the policy floor, full plan size, probes as the budget allows.
UNROUTED_PLAYBOOK = RegimePlaybook(
    "any", "unrouted", Decimal(0), Decimal(1), True, "regime routing off (PRAMANA_REGIME_PLAYBOOKS)",
)

assert set(PLAYBOOKS) == REGIME_LABELS, "every regime label needs a playbook"


def regime_label_of(context) -> str | None:
    """The deterministic regime label an evidence context carried, when it carried one."""
    if context is None:
        return None
    try:
        label = dict(context.regime).get("label")
    except (AttributeError, TypeError, ValueError):
        return None
    return label if isinstance(label, str) and label else None


def playbook_for(label: str | None, *, enabled: bool = True) -> RegimePlaybook:
    if not enabled:
        return UNROUTED_PLAYBOOK
    if label is None:
        return DEFAULT_PLAYBOOK
    return PLAYBOOKS.get(label, DEFAULT_PLAYBOOK)


def conviction_multiplier(confidence: Decimal, *, floor: Decimal) -> Decimal:
    """``FLOOR_SIZE_SHARE`` at the floor, 1 at ``FULL_SIZE_CONFIDENCE``, linear between.

    Clamped at both ends: a decision under the floor (a model stance the deterministic
    floor never judged) is sized like one exactly at it, never smaller, and no conviction
    buys more than the plan allows.
    """
    if confidence <= floor or FULL_SIZE_CONFIDENCE <= floor:
        return FLOOR_SIZE_SHARE.quantize(PLACES)
    if confidence >= FULL_SIZE_CONFIDENCE:
        return Decimal(1).quantize(PLACES)
    progress = (confidence - floor) / (FULL_SIZE_CONFIDENCE - floor)
    return (FLOOR_SIZE_SHARE + (Decimal(1) - FLOOR_SIZE_SHARE) * progress).quantize(PLACES)


def sized_quantity(
    plan_quantity: int, *, confidence: Decimal, floor: Decimal, size_multiplier: Decimal,
) -> tuple[int, dict[str, object]]:
    """The plan-sized entry scaled by conviction and playbook, never below one unit.

    The plan already applied every cap; scaling can only take less. One unit is kept when
    the plan allowed at least one, so sizing never turns an approved entry into a refusal,
    which would hide a directional decision from the journal. Zero stays zero.
    """
    conviction = conviction_multiplier(confidence, floor=floor)
    if plan_quantity <= 0:
        quantity = 0
    else:
        scaled = (Decimal(plan_quantity) * conviction * size_multiplier).to_integral_value(rounding=ROUND_DOWN)
        quantity = max(1, int(scaled))
    return quantity, {
        "plan_quantity": plan_quantity,
        "conviction_multiplier": _fixed(conviction),
        "playbook_multiplier": _fixed(size_multiplier),
        "quantity": quantity,
    }


def sized_from_provenance(plan_quantity: int, confidence: Decimal, provenance: dict | None) -> tuple[int, dict[str, object]] | None:
    """Apply the playbook a decision recorded to its plan-sized quantity; None without one."""
    playbook = (provenance or {}).get("playbook") if isinstance(provenance, dict) else None
    if not isinstance(playbook, dict):
        return None
    try:
        floor = Decimal(str(playbook["floor"]))
        multiplier = Decimal(str(playbook["size_multiplier"]))
    except (KeyError, InvalidOperation, TypeError, ValueError):
        return None
    if not Decimal(0) < multiplier <= Decimal(1) or floor < 0:
        return None
    return sized_quantity(plan_quantity, confidence=confidence, floor=floor, size_multiplier=multiplier)


def describe() -> str:
    """One line per regime for the pre-market check and the manifest."""
    parts = []
    for label in (TRENDING_UP, RANGING, TRENDING_DOWN, HIGH_VOLATILITY, INSUFFICIENT_HISTORY):
        item = PLAYBOOKS[label]
        extra = f" floor+{item.floor_adjustment}" if item.floor_adjustment else ""
        probes = "" if item.probes_allowed else " no-probes"
        parts.append(f"{label}={item.name} x{item.size_multiplier}{extra}{probes}")
    return "; ".join(parts)
