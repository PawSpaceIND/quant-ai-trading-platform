"""What a decision expects to be worth, net of the cost it must clear.

A probability on its own cannot decide anything. 0.55 is a good bet at three-to-one and a
bad one at even money, so a forecast without a payoff beside it is a number that looks
like a reason and is not. This is the other half: the expected value of the decision under
its own recorded probability, its own expected win and loss, and the cost stored with the
forecast.

    EV = p * e_win + (1 - p) * e_loss - cost

Three deliberate choices.

**The inputs are the decision's own.** ``e_win`` and ``e_loss`` are the consensus expected
return and expected risk the specialists already produced, not a house assumption. The
module this replaces on the live path, ``ai.ensemble``, multiplied probability by a fixed
0.03 and a fixed 0.015 - numbers no instrument, regime or book ever justified, and which
made every EV it produced a restatement of the probability.

**The cost is the row's own.** A later change of cost policy must not rescore a decision
made under the old one, so the cost comes from the forecast block rather than from a
current setting.

**Computing it is not acting on it.** Until ``promotion_report()`` passes and the operator
arms the gate, this is recorded and read by nothing. An uncalibrated probability inside a
correct formula produces a confident, wrong number, and the whole point of recording it
now is to find out whether it is worth trusting later.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

SCHEMA = "pramana.expected_value.v1"
# What produced the payoffs this EV was computed from. "declared" is the specialists' own
# expected_return and expected_risk - a labelled convention, identical on every instrument
# in every regime, and the reason break-even sits at 0.70. An empirical source names a
# measurement instead, as "empirical:<artifact id>", and is recorded beside the declared
# number rather than in place of it until something has decided the measurement is worth
# trusting. See quant_ai.analytics.empirical_payoffs.
DECLARED = "declared"
BASIS_POINT = Decimal(10000)
PLACES = Decimal("0.000001")


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, InvalidOperation, TypeError, ValueError):
        return None


def expected_value(
    probability_up: Decimal,
    *,
    expected_return: Decimal,
    expected_risk: Decimal,
    cost_bps: Decimal,
) -> Decimal:
    """The decision's expected return per unit, net of the round-trip cost it must clear.

    ``expected_risk`` is a magnitude, so the losing leg is its negation: a decision that
    is wrong loses, it does not earn a positive risk.
    """
    probability = max(Decimal(0), min(Decimal(1), probability_up))
    win = probability * expected_return
    loss = (Decimal(1) - probability) * (-expected_risk)
    return (win + loss - cost_bps / BASIS_POINT).quantize(PLACES)


def record(
    *,
    forecast: Any,
    expected_return: Any,
    expected_risk: Any,
    empirical: Any = None,
) -> dict[str, Any] | None:
    """The EV block for a decision's provenance, or None when its inputs are incomplete.

    None rather than zero: a decision whose probability or payoff could not be read has no
    expected value, and an EV of zero is a claim that the bet is exactly fair. A gate that
    cannot tell those apart would admit the unknown case as though it had been measured.
    """
    block = forecast if isinstance(forecast, dict) else {}
    probability = _decimal(block.get("probability_up"))
    cost_bps = _decimal(block.get("cost_bps"))
    win, risk = _decimal(expected_return), _decimal(expected_risk)
    if probability is None or cost_bps is None or win is None or risk is None:
        return None
    return {
        "schema": SCHEMA,
        # Every input, so the arithmetic on the page can be checked without the source.
        "probability_up": str(probability),
        "expected_win": str(win),
        "expected_loss": str(-risk),
        "cost_bps": str(cost_bps),
        "expected_value": str(expected_value(
            probability, expected_return=win, expected_risk=risk, cost_bps=cost_bps,
        )),
        # Where the two payoffs came from. Always "declared" on the live path: measuring
        # them is a separate question from acting on the measurement, and a silent swap
        # would restate what every EV already written had meant.
        "e_win_source": DECLARED,
        "e_loss_source": DECLARED,
        # The mapping the probability came from. An EV is only as meaningful as the basis
        # under it, and a refit ships a new id rather than changing what this one meant.
        "basis": block.get("basis"),
        # The same probability and the same cost, priced against measured payoffs, when a
        # measurement that applies to this decision exists. Recorded beside the declared
        # figure, never replacing it, and read by nothing.
        **({"ev_empirical": measured} if (measured := _measured(
            probability, cost_bps, empirical)) is not None else {}),
    }


def _measured(probability: Decimal, cost_bps: Decimal, empirical: Any) -> dict[str, Any] | None:
    """The EV this decision would have had under measured payoffs, or None.

    None whenever the measurement is absent, unreadable or names no source. An empirical
    block that silently fell back to the declared numbers would be the declared EV wearing
    a second name, which is worse than no second number at all.
    """
    if not isinstance(empirical, dict):
        return None
    win = _decimal(empirical.get("e_win_hat"))
    risk = _decimal(empirical.get("e_loss_hat"))
    source = empirical.get("source")
    if win is None or risk is None or not isinstance(source, str) or not source:
        return None
    return {
        "expected_win": str(win),
        "expected_loss": str(-risk),
        "expected_value": str(expected_value(
            probability, expected_return=win, expected_risk=risk, cost_bps=cost_bps,
        )),
        "e_win_source": source,
        "e_loss_source": source,
        "sample": empirical.get("n"),
    }
