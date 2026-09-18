"""One gate a candidate has to pass before anyone is allowed to be impressed by it.

The three statistics are useless apart. A great deflated Sharpe on 40 observations is not
evidence; a long track record whose selection procedure fails CSCV is not evidence either.
This runs all of them together and returns a single verdict with the reasons attached.

It fails closed in the one place that matters: the number of candidates the search
evaluated is a REQUIRED input and there is no default. Defaulting it to one would silently
restore exactly the bias the module exists to remove, and a sweep of four hundred variants
would come out looking like a single pre-registered hypothesis. If the trial register does
not know, the answer is that the study is not gradeable — not that it passed.

Nothing here approves anything. Clearing a statistical gate means a result is not obviously
noise. It says nothing about capacity, regime coverage, execution realism or whether the
data the study consumed was itself trustworthy, and it is not permission to trade.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import fmean

from quant_ai.marketdata.point_in_time import SurvivorshipAudit
from quant_ai.validation.deflated_sharpe import (
    deflated_sharpe_ratio,
    expected_maximum_sharpe,
    minimum_track_record_length,
    sample_moments,
    sharpe_ratio,
)
from quant_ai.validation.overfitting import (
    OverfittingReport,
    probability_of_backtest_overfitting,
)
from quant_ai.validation.trial_register import register_summary

SCHEMA = "pramana.validation_harness.v1"

LIMITATION = (
    "A cleared statistical gate means the result is not obviously selection noise. It is not "
    "evidence of capacity, regime coverage, execution realism or data quality, and it is not "
    "permission to trade."
)


@dataclass(frozen=True)
class ValidationThresholds:
    minimum_deflated_sharpe: float = 0.95
    maximum_overfitting_probability: float = 0.10
    track_record_confidence: float = 0.95


@dataclass(frozen=True)
class ValidationVerdict:
    clears_statistical_gate: bool
    reasons: tuple[str, ...]
    observed_sharpe: float
    deflated_sharpe: float
    selection_benchmark_sharpe: float
    minimum_track_record: float
    observations: int
    trials: int
    overfitting: OverfittingReport | None
    universe: SurvivorshipAudit | None

    def as_evidence(self) -> dict[str, object]:
        report = {
            "schema": SCHEMA,
            "clears_statistical_gate": self.clears_statistical_gate,
            "reasons": list(self.reasons),
            "observed_sharpe": self.observed_sharpe,
            "deflated_sharpe": self.deflated_sharpe,
            "selection_benchmark_sharpe": self.selection_benchmark_sharpe,
            "minimum_track_record": self.minimum_track_record,
            "observations": self.observations,
            "trials": self.trials,
            "limitation": LIMITATION,
        }
        if self.universe is not None:
            report["universe"] = self.universe.as_evidence()
        if self.overfitting is not None:
            report["overfitting"] = {
                "probability": self.overfitting.probability,
                "verdict": self.overfitting.verdict,
                "splits": self.overfitting.splits,
                "strategies": self.overfitting.strategies,
            }
        return report


def trial_sharpe_variance(sharpes: Sequence[float]) -> float:
    """Variance of the Sharpes a sweep produced — the scale the selection bias is measured in.

    Estimate this from the candidates that were actually evaluated, including the ones that
    did badly. Dropping the failures shrinks the variance and therefore the bar, which is
    the same mistake as not counting the trials.
    """
    values = [float(value) for value in sharpes]
    if len(values) < 2:
        raise ValueError("trial sharpe variance needs at least two evaluated candidates")
    mean = fmean(values)
    return math.fsum((value - mean) ** 2 for value in values) / (len(values) - 1)


def trials_from_register(path: str | Path, *, study: str) -> int:
    """Cumulative candidate count for a study, read from the hash-chained register."""
    summary = register_summary(path, study=study)
    trials = int(summary["candidate_trials"])
    if trials < 1:
        raise ValueError(f"trial register records no candidates for study {study!r}")
    return trials


def validate_candidate(
    returns: Sequence[float],
    *,
    trials: int,
    candidate_sharpes: Sequence[float],
    label_span: int = 0,
    performance_matrix: Sequence[Sequence[float]] | None = None,
    universe_audit: SurvivorshipAudit | None = None,
    overfitting_chunks: int = 8,
    thresholds: ValidationThresholds | None = None,
) -> ValidationVerdict:
    """Grade one candidate against the search that produced it.

    ``candidate_sharpes`` is every Sharpe the sweep produced, not just the survivors.
    ``performance_matrix`` is one row per observation and one column per candidate; when it
    is supplied the CSCV overfitting test runs too, and when it is absent the verdict says
    so rather than passing silently on two statistics out of three.

    ``universe_audit`` is the survivorship check on the data the study consumed. A study run
    on a universe assembled from the companies that still exist clears every statistic in
    this module and is still wrong, because the bias is in the input rather than the
    estimator. Omitting the audit is therefore reported as a reason, not waved through.
    """
    limits = thresholds or ValidationThresholds()
    variance = trial_sharpe_variance(candidate_sharpes)

    moments = sample_moments(returns)
    observed = sharpe_ratio(returns)
    benchmark = expected_maximum_sharpe(trials, variance)
    deflated = deflated_sharpe_ratio(returns, trials=trials, trial_sharpe_variance=variance)
    needed = minimum_track_record_length(
        returns,
        benchmark_sharpe=benchmark,
        confidence=limits.track_record_confidence,
    )

    reasons: list[str] = []
    if deflated < limits.minimum_deflated_sharpe:
        reasons.append(
            f"deflated sharpe {deflated:.3f} is below {limits.minimum_deflated_sharpe:.2f}; "
            f"a search of {trials} candidates would be expected to produce a sharpe of "
            f"{benchmark:.3f} with no edge at all"
        )
    if math.isinf(needed):
        reasons.append(
            f"no track record length could make this significant: the observed sharpe "
            f"{observed:.3f} does not exceed the {benchmark:.3f} bar a search of {trials} "
            f"candidates sets"
        )
    elif moments.count < needed:
        reasons.append(
            f"{moments.count} observations is short of the {math.ceil(needed)} required for "
            f"this sharpe to be significant at {limits.track_record_confidence:.0%}"
        )

    if universe_audit is None:
        reasons.append(
            "the universe this study consumed was never audited for survivorship, so an "
            "upward bias in the input cannot be ruled out by any statistic here"
        )
    elif not universe_audit.usable_for_research:
        detail = universe_audit.reasons[0] if universe_audit.reasons else universe_audit.verdict
        reasons.append(f"universe is not research-grade ({universe_audit.verdict}): {detail}")

    report: OverfittingReport | None = None
    if performance_matrix is None:
        reasons.append(
            "no performance matrix was supplied, so the selection procedure was never tested "
            "for overfitting"
        )
    else:
        report = probability_of_backtest_overfitting(
            performance_matrix, chunks=overfitting_chunks
        )
        if report.probability > limits.maximum_overfitting_probability:
            reasons.append(
                f"probability of backtest overfitting {report.probability:.2f} exceeds "
                f"{limits.maximum_overfitting_probability:.2f} ({report.verdict})"
            )

    if label_span > 0 and performance_matrix is None:
        reasons.append(
            f"labels span {label_span} observations, so folds must be purged and embargoed; "
            "no cross-validated evidence was supplied"
        )

    return ValidationVerdict(
        clears_statistical_gate=not reasons,
        reasons=tuple(reasons),
        observed_sharpe=observed,
        deflated_sharpe=deflated,
        selection_benchmark_sharpe=benchmark,
        minimum_track_record=needed,
        observations=moments.count,
        trials=trials,
        overfitting=report,
        universe=universe_audit,
    )
