"""Telling a re-based quote apart from a catastrophic gap, as far as that is honest.

A split, bonus or consolidation cuts the quoted price without changing what a position
is worth, so the stored stop and cost basis stop being comparable with it. A fraud
disclosure, a war or a failed result cuts the quoted price because the position really is
worth less, and that is precisely when the stop has to act. Both arrive as one step far
larger than the exchange band, and a size threshold alone cannot separate them: a 1:5
split and a company that loses four fifths of its value produce the identical step of
0.8.

So this module refuses to pretend. It returns a verdict plus the evidence behind it, and
only one verdict claims the question is settled:

``ORDINARY``
    The step is inside the band. Ordinary trading, whatever its size felt like.

``DECLARED_ACTION``
    The operator's own calendar names an ex-date for this symbol on this session. The
    only resolved discontinuity, because it is the only one backed by a declaration
    rather than an inference.

``INTRASESSION_BREAK``
    A step over the band between two marks with no session boundary between them. An
    action re-bases at the open, so this is not one - but a crash, a bad print and a fat
    finger are all still on the table, and nothing here can rank them.

``UNDETERMINED``
    A step over the band across a session boundary that nobody declared. A split and a
    crash are genuinely indistinguishable here.

Why roundness is reported and not believed. A corporate action re-bases by an exact
rational ratio, so it is tempting to read a round ratio as proof of one. It is not. The
first price that trades after an ex-date is the theoretical price *plus that day's real
move*, so a genuine 1:5 split routinely prints 0.19 or 0.21 rather than 0.20. Widening
the tolerance enough to catch those makes the plausible ratios tile the whole line -
every ratio between about 0.1 and 0.96 lands within one exchange band of some small
integer ratio - at which point roundness has stopped discriminating and is only
laundering a guess. ``nearest_action`` therefore travels with the verdict as evidence for
the operator reading the alert, and never as a decision the engine makes for itself.

Nothing here reads a clock, a feed or a file, and nothing raises: the callers are a
one-second protection sweep and an alert path, and a classifier that raised would cost
the sweep it was meant to inform.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, DecimalException
from enum import Enum
from math import gcd

# NSE price bands top out at 20% for most scrips, so a larger single step in a quoted
# price is not trading. Deliberately the same number the re-based-quote guard uses: one
# band, so a step can never be a discontinuity to one component and a real move to the
# other.
DEFAULT_BAND_FRACTION = Decimal("0.20")

# How close an observed ratio must sit to an exact action ratio before it is worth
# putting in front of an operator. Half a percent, because the point of the figure is
# that a free market does not land on 0.200000 by accident; a tolerance wide enough to
# catch the splits that open off-theoretical would match nearly every ratio and say
# nothing. Missing most real actions is the acceptable failure here - the verdict is
# unresolved and loud either way, and only the operator's note in the alert changes.
DEFAULT_RATIO_TOLERANCE = Decimal("0.005")

# Bounds on the action ratios considered. Twenty covers every split, bonus and
# consolidation a listed equity realistically declares (1:20 is about as deep as they go).
#
# ``MAX_MINOR_TERM`` is the one that matters. A real action always has one small side:
# "one new share for every five held", "five for one". Nobody declares eleven shares for
# every seven. Allowing every coprime pair up to twenty admits ratios like 7:11 = 0.6364,
# which no issuer has declared but which sits half a percent from a 36% crash - and 208
# such candidates match a random crash ratio one time in two, at which point the roundness
# figure carries no information at all. Requiring one term at five or below leaves 117
# candidates matching one time in five: still a coincidence an operator will sometimes
# see, which is exactly why the verdict never turns on it, but rare enough to be worth
# putting in the alert. (Measured over 200,000 ratios drawn uniformly from 0.05 to 0.80.)
MAX_ACTION_TERM = 20
MAX_MINOR_TERM = 5


class GapVerdict(str, Enum):
    ORDINARY = "ORDINARY"
    DECLARED_ACTION = "DECLARED_ACTION"
    INTRASESSION_BREAK = "INTRASESSION_BREAK"
    UNDETERMINED = "UNDETERMINED"


@dataclass(frozen=True)
class GapAssessment:
    """One classified price step, with the evidence that produced the verdict.

    ``ratio`` is ``current / previous``; ``step_fraction`` its distance from 1, which is
    what the band is compared against. ``nearest_action`` is the closest plausible
    corporate-action ratio rendered as ``"1:5"`` (new price is one fifth of the old), or
    empty when no candidate sits within tolerance.
    """

    verdict: GapVerdict
    step_fraction: Decimal
    ratio: Decimal
    nearest_action: str = ""
    detail: str = ""

    @property
    def resolved(self) -> bool:
        """Whether the engine knows what this step was.

        Only an ordinary move and an operator's own declaration qualify. Everything else
        is an open question, and an open question about a protected position is something
        an operator has to be told about rather than something the engine may sit on.
        """
        return self.verdict in {GapVerdict.ORDINARY, GapVerdict.DECLARED_ACTION}

    def describe(self) -> str:
        """One line for an alert or a log: verdict, size, and the roundness evidence."""
        note = f"; consistent with {self.nearest_action}" if self.nearest_action else ""
        return f"{self.verdict.value} step={self.step_fraction} ratio={self.ratio}{note}"


def _action_ratios() -> tuple[tuple[Decimal, str], ...]:
    """Every re-basing ratio a small-integer action could produce, outside the band.

    ``q/p`` in lowest terms with one term at ``MAX_MINOR_TERM`` or below and the other at
    ``MAX_ACTION_TERM`` or below: ratios under one are splits and bonuses, over one are
    consolidations. Ratios inside the band are dropped because a step that small is never
    classified as a discontinuity in the first place.
    """
    candidates: list[tuple[Decimal, str]] = []
    for denominator in range(1, MAX_ACTION_TERM + 1):
        for numerator in range(1, MAX_ACTION_TERM + 1):
            if gcd(numerator, denominator) != 1:
                continue
            if min(numerator, denominator) > MAX_MINOR_TERM:
                continue
            ratio = Decimal(numerator) / Decimal(denominator)
            if abs(ratio - Decimal(1)) <= DEFAULT_BAND_FRACTION:
                continue
            candidates.append((ratio, f"{numerator}:{denominator}"))
    return tuple(candidates)


# Enumerated once: the set depends on nothing but the constants above, and the sweep that
# consults it runs every second.
ACTION_RATIOS = _action_ratios()


def nearest_action_ratio(
    ratio: Decimal, *, tolerance: Decimal = DEFAULT_RATIO_TOLERANCE
) -> str:
    """The closest plausible action ratio within ``tolerance``, or an empty string.

    Distance is relative, so a 5:1 consolidation is judged on the same terms as a 1:5
    split rather than being flattered by its larger absolute ratio.
    """
    try:
        if ratio <= 0 or tolerance <= 0:
            return ""
        best, best_error = "", None
        for candidate, label in ACTION_RATIOS:
            error = abs(ratio - candidate) / candidate
            if error <= tolerance and (best_error is None or error < best_error):
                best, best_error = label, error
        return best
    except (DecimalException, ArithmeticError, TypeError):
        return ""


def classify_gap(
    previous: Decimal | None,
    current: Decimal | None,
    *,
    crossed_session_boundary: bool,
    declared_action: str | None = None,
    band: Decimal = DEFAULT_BAND_FRACTION,
    tolerance: Decimal = DEFAULT_RATIO_TOLERANCE,
) -> GapAssessment:
    """Classify the step from ``previous`` to ``current``.

    ``crossed_session_boundary`` says whether a regular session closed between the two
    observations; the caller owns the calendar, so this stays a pure function of its
    arguments. ``declared_action`` is the operator's declaration for this symbol on this
    session, when they made one.

    An unusable input - a first observation, a non-positive price, a nonsensical band -
    is reported as ``ORDINARY`` with a stated reason, never as a discontinuity. Reading a
    missing previous price as a gap would suspend judgement on every position at every
    restart; reading it as ordinary leaves the stop exactly as armed as it already was.
    """
    try:
        if previous is None or current is None or previous <= 0 or current <= 0:
            return GapAssessment(
                GapVerdict.ORDINARY, Decimal(0), Decimal(1), "", "no_comparable_previous_mark"
            )
        if band <= 0:
            return GapAssessment(GapVerdict.ORDINARY, Decimal(0), Decimal(1), "", "invalid_band")
        ratio = current / previous
        step = abs(ratio - Decimal(1))
        if declared_action:
            return GapAssessment(
                GapVerdict.DECLARED_ACTION, step, ratio,
                nearest_action_ratio(ratio, tolerance=tolerance),
                f"declared:{str(declared_action)[:40]}",
            )
        if step <= band:
            return GapAssessment(GapVerdict.ORDINARY, step, ratio, "", "within_band")
        nearest = nearest_action_ratio(ratio, tolerance=tolerance)
        if not crossed_session_boundary:
            # An action re-bases at an open. Two marks inside one session cannot be
            # separated by one, so whatever this was, it was not a corporate action.
            return GapAssessment(
                GapVerdict.INTRASESSION_BREAK, step, ratio, nearest, "no_session_boundary"
            )
        return GapAssessment(GapVerdict.UNDETERMINED, step, ratio, nearest, "undeclared_step")
    except (DecimalException, ArithmeticError, TypeError):
        # A classifier that raised would take down the sweep it exists to inform. An
        # arithmetic fault is not evidence of a discontinuity, so it reads as ordinary
        # and leaves every protective level exactly as armed as it already was.
        return GapAssessment(
            GapVerdict.ORDINARY, Decimal(0), Decimal(1), "", "classification_failed"
        )
