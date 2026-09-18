"""Whether a live or paper record has run long enough to mean anything, and whether it agrees.

A backtest is a claim. A track record is the only evidence that can settle it, and the
question people skip is how much of one is needed. Forty profitable sessions feel like
proof and are not: at a daily Sharpe consistent with a good strategy, forty observations
cannot distinguish genuine edge from a coin that landed well.

Two questions are answered here and they are not the same.

**Is the record long enough?** :func:`minimum_track_record_length` gives the observations
needed before the observed Sharpe could be significant at all. Below it no verdict on the
strategy is available — not "it is working", not "it is broken". The honest output is a
number of sessions still to run.

**Does it agree with the backtest?** A strategy promoted on a backtest Sharpe of 1.4 that
delivers 0.2 live has not had bad luck, it was overfit, and the deflated Sharpe that let it
through was computed on a search whose size was understated. That is the check that closes
the loop between research and reality, and without it a promotion gate is a one-way door.

Degradation is expected and is not by itself failure: live trading pays costs and delays a
backtest can only estimate. What matters is whether the shortfall is larger than sampling
noise explains.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from quant_ai.validation.deflated_sharpe import (
    minimum_track_record_length,
    probabilistic_sharpe_ratio,
    sample_moments,
)

SCHEMA = "pramana.track_record.v1"

#: Sessions below which nothing is judged, whatever the statistics say. A month of trading
#: can look like anything, and a verdict offered on it would be replaced by a different one
#: a fortnight later.
MINIMUM_SESSIONS = 60

#: Confidence for both the "is it real" and the "does it match research" tests.
DEFAULT_CONFIDENCE = 0.95

TRADING_SESSIONS_A_YEAR = 252

NO_RECORD_REASON = (
    "{sessions} sessions. There is no track record yet, so the backtest remains an "
    "unchecked claim."
)


@dataclass(frozen=True)
class TrackRecordVerdict:
    sessions: int
    realised_sharpe: float
    annualised_sharpe: float
    backtest_sharpe: float
    probabilistic_sharpe: float
    minimum_sessions_needed: float
    sessions_remaining: float
    shortfall_against_backtest: float
    shortfall_is_significant: bool
    verdict: str
    reasons: tuple

    @property
    def ready_to_judge(self) -> bool:
        return self.verdict != "too_short"

    @property
    def supports_going_live(self) -> bool:
        """Only a record that is both long enough and consistent with its research."""
        return self.verdict == "consistent_with_research"

    def as_evidence(self) -> dict:
        return {
            "schema": SCHEMA,
            "sessions": self.sessions,
            "realised_sharpe": round(self.realised_sharpe, 4),
            "annualised_sharpe": round(self.annualised_sharpe, 3),
            "backtest_sharpe": round(self.backtest_sharpe, 3),
            "probabilistic_sharpe": round(self.probabilistic_sharpe, 4),
            "minimum_sessions_needed": (
                None
                if math.isinf(self.minimum_sessions_needed)
                else round(self.minimum_sessions_needed, 1)
            ),
            "sessions_remaining": (
                None
                if math.isinf(self.sessions_remaining)
                else round(self.sessions_remaining, 1)
            ),
            "shortfall_against_backtest": round(self.shortfall_against_backtest, 4),
            "shortfall_is_significant": self.shortfall_is_significant,
            "verdict": self.verdict,
            "ready_to_judge": self.ready_to_judge,
            "supports_going_live": self.supports_going_live,
            "reasons": list(self.reasons),
            "limitation": (
                "Measures one record against one backtest claim. It cannot tell a strategy "
                "that stopped working from a market that changed, and a record that agrees "
                "with its research is not evidence the research was right about why."
            ),
        }


def evaluate_track_record(
    returns: Sequence[float],
    *,
    backtest_sharpe: float,
    minimum_sessions: int = MINIMUM_SESSIONS,
    confidence: float = DEFAULT_CONFIDENCE,
) -> TrackRecordVerdict:
    """Grade realised per-session returns against the backtest that justified trading them.

    ``backtest_sharpe`` is the *per-session* Sharpe the research claimed, on the same
    footing as ``returns``. Passing an annualised figure against daily returns compares two
    different quantities and would make every record look catastrophic.
    """
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence must be strictly between 0 and 1")
    if minimum_sessions < 2:
        raise ValueError("a track record needs at least two sessions to have a dispersion")

    observed = [float(value) for value in returns]
    sessions = len(observed)
    if sessions < 2:
        return TrackRecordVerdict(
            sessions=sessions,
            realised_sharpe=0.0,
            annualised_sharpe=0.0,
            backtest_sharpe=backtest_sharpe,
            probabilistic_sharpe=0.0,
            minimum_sessions_needed=math.inf,
            sessions_remaining=math.inf,
            shortfall_against_backtest=0.0,
            shortfall_is_significant=False,
            verdict="too_short",
            reasons=(NO_RECORD_REASON.format(sessions=sessions),),
        )

    moments = sample_moments(observed)
    realised = moments.mean / moments.stdev
    annualised = realised * math.sqrt(TRADING_SESSIONS_A_YEAR)
    psr = probabilistic_sharpe_ratio(observed, 0.0)
    needed = minimum_track_record_length(observed, benchmark_sharpe=0.0, confidence=confidence)
    remaining = math.inf if math.isinf(needed) else max(0.0, needed - sessions)
    shortfall = realised - backtest_sharpe

    # Is the record below its backtest by more than sampling noise explains? The standard
    # error of a Sharpe over n observations is approximately sqrt((1 + S^2/2)/n).
    standard_error = math.sqrt((1.0 + realised**2 / 2.0) / sessions)
    significant = shortfall < 0.0 and abs(shortfall) > standard_error * _quantile(confidence)

    reasons: list = []
    if sessions < minimum_sessions:
        verdict = "too_short"
        reasons.append(
            f"{sessions} of {minimum_sessions} sessions. Nothing is concluded yet — not that "
            "it works, not that it is broken. A month of trading can look like anything."
        )
    elif math.isinf(needed):
        # Ahead of degradation: a strategy that does not beat zero is not "underperforming
        # its research", it is not working. Calling that degraded would understate it.
        verdict = "no_edge"
        reasons.append(
            f"over {sessions} sessions the realised Sharpe is {realised:.3f} per session "
            f"({annualised:.2f} annualised) and does not exceed zero. No further data makes "
            "a claim that is not being made."
        )
    elif significant:
        # Ahead of the remaining length test. Whether the record has established an edge and
        # whether it has fallen short of its research are different questions, and the
        # second can be answered sooner. Reporting "run another 470 sessions" to someone
        # whose strategy is already measurably below its backtest costs them two years.
        verdict = "degraded"
        reasons.append(
            f"realised {realised:.3f} per session against {backtest_sharpe:.3f} claimed by "
            f"research, a shortfall of {abs(shortfall):.3f} larger than the "
            f"{standard_error:.3f} standard error explains. This is not bad luck: the "
            "backtest was optimistic, and the search that produced it was larger than it "
            "was charged for. Stop and re-examine rather than waiting for more sessions."
        )
        if math.isinf(needed) or sessions < needed:
            reasons.append(
                "The record is also too short to establish an edge on its own, so the "
                "shortfall is the only conclusion available - not that the strategy is "
                "profitable but weaker than advertised."
            )
    elif sessions < needed:
        verdict = "too_short"
        reasons.append(
            f"{sessions} sessions at a realised Sharpe of {annualised:.2f} annualised needs "
            f"{needed:.0f} before it could be significant at {confidence:.0%}: "
            f"{remaining:.0f} more sessions, about "
            f"{remaining / 21:.1f} trading months."
        )
    else:
        verdict = "consistent_with_research"
        reasons.append(
            f"{sessions} sessions at {annualised:.2f} annualised, significant at "
            f"{confidence:.0%}, and within sampling error of the {backtest_sharpe:.3f} per "
            "session research claimed."
        )
    if psr < confidence and verdict == "consistent_with_research":
        reasons.append(
            f"The probabilistic Sharpe is {psr:.3f}, below {confidence:.2f}: the record "
            "agrees with its backtest but has not on its own established an edge."
        )

    return TrackRecordVerdict(
        sessions=sessions,
        realised_sharpe=realised,
        annualised_sharpe=annualised,
        backtest_sharpe=backtest_sharpe,
        probabilistic_sharpe=psr,
        minimum_sessions_needed=needed,
        sessions_remaining=remaining,
        shortfall_against_backtest=shortfall,
        shortfall_is_significant=significant,
        verdict=verdict,
        reasons=tuple(reasons),
    )


def _quantile(confidence: float) -> float:
    from statistics import NormalDist

    return NormalDist().inv_cdf(confidence)
