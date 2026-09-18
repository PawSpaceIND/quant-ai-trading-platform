"""Measure what execution actually cost against what the friction model said it would.

Every backtest in this repository is priced by :class:`MarketFrictionModel` — bounded
spread, square-root impact, broker charges, statutory fees. The fee half of that is exact:
brokerage, STT, stamp duty and GST come from published schedules and there is nothing to
calibrate. The *impact and spread* half is a model, and a model nobody has checked against
a fill is an assumption wearing a number.

This matters more than it sounds. Cost enters a study net of everything, so a model that
understates impact by a few basis points promotes strategies whose entire edge is the
error. That failure is invisible in the backtest, invisible in the deflated Sharpe, and
appears only as a live track record that does not resemble the research. It is the single
most common way a validated strategy loses money.

So the comparison is narrow and deliberate: realised drag against modelled drag, per fill,
paired. Cash charges are excluded because they are known exactly and including them would
dilute the statistic with a term that cannot be wrong.

**With no fills this reports ``insufficient_evidence``, and says so rather than
``calibrated``.** An unmeasured model is not a correct one, and a report that let silence
read as agreement would be the same lie in a different place.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from statistics import NormalDist

SCHEMA = "pramana.cost_calibration.v1"

_NORMAL = NormalDist()

#: Below this many fills no verdict is offered. Paired drag is noisy — one fill into a
#: thin book can swamp twenty ordinary ones — and a calibration declared on a handful of
#: trades would be replaced by a different conclusion next week.
MINIMUM_FILLS = 30

#: The bias that matters. A model wrong by less than this on a round trip is not the reason
#: a strategy did or did not work; one wrong by more can invent an edge on its own.
MATERIAL_BIAS_BPS = 5.0

#: Two-sided significance for calling the model wrong rather than the sample noisy.
DEFAULT_CONFIDENCE = 0.95

BUY = "BUY"
SELL = "SELL"

NO_FILLS_REASON = (
    "{count} fills. The friction model that prices every backtest in this repository has "
    "never been checked against an execution, so its spread and impact terms are "
    "assumptions. Nothing here says they are wrong; nothing says they are right either."
)


@dataclass(frozen=True)
class RealisedFill:
    """One execution, with the price the decision was made at and the price it filled at.

    ``reference_price`` is the arrival price — what the book showed when the order was
    sent. Measuring against anything later (the day's close, the VWAP) folds the market's
    own movement into the cost and makes a slow fill look free in a rising market.

    ``modelled_execution_price`` is what :class:`MarketFrictionModel` predicted for this
    order, so the two numbers are commensurable by construction.
    """

    symbol: str
    side: str
    quantity: Decimal
    reference_price: Decimal
    fill_price: Decimal
    modelled_execution_price: Decimal
    traded_on: date

    def __post_init__(self) -> None:
        if self.side not in (BUY, SELL):
            raise ValueError(f"{self.symbol}: side must be {BUY} or {SELL}")
        if self.quantity <= 0:
            raise ValueError(f"{self.symbol}: a fill needs a positive quantity")
        for name in ("reference_price", "fill_price", "modelled_execution_price"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{self.symbol}: {name} must be positive")

    def _drag(self, price: Decimal) -> float:
        """Cost as a positive fraction: paying above reference, or selling below it."""
        move = (price - self.reference_price) / self.reference_price
        return float(move if self.side == BUY else -move)

    @property
    def realised_drag_bps(self) -> float:
        return self._drag(self.fill_price) * 10_000.0

    @property
    def modelled_drag_bps(self) -> float:
        return self._drag(self.modelled_execution_price) * 10_000.0

    @property
    def error_bps(self) -> float:
        """Positive means execution cost more than the model predicted."""
        return self.realised_drag_bps - self.modelled_drag_bps

    @property
    def notional(self) -> Decimal:
        return self.quantity * self.reference_price


@dataclass(frozen=True)
class CostCalibration:
    fills: int
    realised_bps: float
    modelled_bps: float
    mean_error_bps: float
    error_dispersion_bps: float
    t_statistic: float
    confidence_the_model_is_wrong: float
    minimum_fills: int
    fills_needed: float
    verdict: str
    reasons: tuple

    @property
    def model_is_usable(self) -> bool:
        return self.verdict in {"calibrated", "model_overstates_cost"}

    @property
    def backtests_are_invalidated(self) -> bool:
        """Whether every passing backtest was priced on a model that charged too little."""
        return self.verdict == "model_understates_cost"

    def as_evidence(self) -> dict:
        return {
            "schema": SCHEMA,
            "fills": self.fills,
            "realised_bps": round(self.realised_bps, 3),
            "modelled_bps": round(self.modelled_bps, 3),
            "mean_error_bps": round(self.mean_error_bps, 3),
            "error_dispersion_bps": round(self.error_dispersion_bps, 3),
            "t_statistic": round(self.t_statistic, 3),
            "confidence_the_model_is_wrong": round(self.confidence_the_model_is_wrong, 4),
            "minimum_fills": self.minimum_fills,
            "fills_needed": (
                None if math.isinf(self.fills_needed) else round(self.fills_needed, 1)
            ),
            "verdict": self.verdict,
            "model_is_usable": self.model_is_usable,
            "backtests_are_invalidated": self.backtests_are_invalidated,
            "reasons": list(self.reasons),
            "limitation": (
                "Compares realised against modelled spread and impact only. Broker and "
                "statutory charges are excluded: they come from published schedules, are "
                "exact, and including them would dilute the statistic with a term that "
                "cannot be wrong."
            ),
        }


def fills_needed_to_detect(
    dispersion_bps: float,
    *,
    bias_bps: float = MATERIAL_BIAS_BPS,
    confidence: float = DEFAULT_CONFIDENCE,
) -> float:
    """How many fills before a bias of ``bias_bps`` would be distinguishable from noise.

    The execution counterpart of a minimum track record length, and it answers the same
    question: not "is the model right" but "have we traded enough to be entitled to an
    opinion". Returns infinity when the dispersion is such that no finite sample settles it.
    """
    if dispersion_bps < 0.0:
        raise ValueError("dispersion cannot be negative")
    if bias_bps <= 0.0:
        raise ValueError("the bias to detect must be positive")
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence must be strictly between 0 and 1")
    if dispersion_bps == 0.0:
        return 1.0
    quantile = _NORMAL.inv_cdf(1.0 - (1.0 - confidence) / 2.0)
    return (quantile * dispersion_bps / bias_bps) ** 2


def calibrate_costs(
    fills: Sequence,
    *,
    minimum_fills: int = MINIMUM_FILLS,
    material_bias_bps: float = MATERIAL_BIAS_BPS,
    confidence: float = DEFAULT_CONFIDENCE,
) -> CostCalibration:
    """Grade the friction model against realised executions.

    The test is paired: each fill contributes one error, realised minus modelled, so an
    instrument's own volatility cancels instead of being mistaken for model bias.
    """
    if minimum_fills < 2:
        raise ValueError("a calibration needs at least two fills to have a dispersion")
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence must be strictly between 0 and 1")

    observed = list(fills)
    count = len(observed)
    if count < 2:
        return CostCalibration(
            fills=count,
            realised_bps=0.0,
            modelled_bps=0.0,
            mean_error_bps=0.0,
            error_dispersion_bps=0.0,
            t_statistic=0.0,
            confidence_the_model_is_wrong=0.0,
            minimum_fills=minimum_fills,
            fills_needed=math.inf,
            verdict="insufficient_evidence",
            reasons=(NO_FILLS_REASON.format(count=count),),
        )

    errors = [item.error_bps for item in observed]
    realised = sum(item.realised_drag_bps for item in observed) / count
    modelled = sum(item.modelled_drag_bps for item in observed) / count
    mean_error = sum(errors) / count
    variance = sum((value - mean_error) ** 2 for value in errors) / (count - 1)
    dispersion = math.sqrt(variance)

    if dispersion == 0.0:
        t_statistic = math.inf if mean_error != 0.0 else 0.0
    else:
        t_statistic = mean_error / (dispersion / math.sqrt(count))
    wrongness = (
        1.0 if math.isinf(t_statistic)
        else 2.0 * _NORMAL.cdf(abs(t_statistic)) - 1.0
    )
    needed = fills_needed_to_detect(
        dispersion, bias_bps=material_bias_bps, confidence=confidence
    )

    reasons: list = []
    if count < minimum_fills:
        verdict = "insufficient_evidence"
        reasons.append(
            f"{count} fills is below the {minimum_fills} this refuses to judge on. Paired "
            "drag is noisy — one fill into a thin book swamps twenty ordinary ones — and a "
            "verdict declared here would be replaced by a different one next week."
        )
    elif wrongness < confidence:
        verdict = "calibrated"
        reasons.append(
            f"realised drag {realised:.2f}bps against {modelled:.2f}bps modelled; the "
            f"{mean_error:+.2f}bps difference is not distinguishable from sampling noise at "
            f"{confidence:.0%}. The model is not confirmed correct, only not caught wrong."
        )
    elif mean_error > 0.0:
        verdict = "model_understates_cost"
        reasons.append(
            f"execution cost {mean_error:+.2f}bps more than the model charged "
            f"({realised:.2f} against {modelled:.2f}), significant at {confidence:.0%}. "
            "Every backtest that passed was priced too cheaply by this margin, so any "
            "strategy whose edge is smaller than it was never real."
        )
    else:
        verdict = "model_overstates_cost"
        reasons.append(
            f"execution cost {mean_error:+.2f}bps less than the model charged "
            f"({realised:.2f} against {modelled:.2f}). Backtests were priced conservatively, "
            "which is the safe direction to be wrong in, but it hides strategies whose real "
            "edge clears the true cost."
        )
    if abs(mean_error) < material_bias_bps and verdict.startswith("model_"):
        reasons.append(
            f"The bias is statistically real but smaller than the {material_bias_bps:.1f}bps "
            "that would change which strategies pass."
        )

    return CostCalibration(
        fills=count,
        realised_bps=realised,
        modelled_bps=modelled,
        mean_error_bps=mean_error,
        error_dispersion_bps=dispersion,
        t_statistic=t_statistic,
        confidence_the_model_is_wrong=wrongness,
        minimum_fills=minimum_fills,
        fills_needed=needed,
        verdict=verdict,
        reasons=tuple(reasons),
    )
