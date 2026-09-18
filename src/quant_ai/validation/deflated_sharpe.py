"""Sharpe ratio statistics that account for how hard you looked.

A Sharpe ratio computed from a backtest answers the wrong question. It says "this is how
well the strategy did on the data I selected it with", and the more candidates the search
evaluated, the better that number is *by construction* — the maximum of N draws from a
zero-mean distribution rises with N whether or not any of them has edge.

Three statistics from Bailey and Lopez de Prado close that gap:

* ``probabilistic_sharpe_ratio`` — the probability that the true Sharpe exceeds a
  benchmark, given the sample length and the non-normality of the returns. Fat tails and
  negative skew, which every real return series has, make an observed Sharpe less
  trustworthy than a normal assumption suggests.
* ``deflated_sharpe_ratio`` — the same probability measured against the Sharpe the *best
  of N random candidates* would be expected to show. This is the number that answers
  "would a coin-flipping search have produced this?".
* ``minimum_track_record_length`` — how many observations are needed before the claim
  could be significant at all. Usually the honest answer to a promising short backtest.

These are floats, not Decimals. Decimal is used in this codebase for money, where exact
representation is the point; these are estimated probabilities from sampled moments, where
implying exactness would be a lie.

Reference: Bailey, D. and Lopez de Prado, M. (2014), "The Deflated Sharpe Ratio:
Correcting for Selection Bias, Backtest Overfitting and Non-Normality".
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import NormalDist

SCHEMA = "pramana.deflated_sharpe.v1"

_NORMAL = NormalDist()
_EULER_MASCHERONI = 0.5772156649015329


@dataclass(frozen=True)
class SampleMoments:
    """The four moments the statistics below need, estimated once."""

    count: int
    mean: float
    stdev: float
    skewness: float
    kurtosis: float
    """Non-excess kurtosis: a normal distribution scores 3.0, not 0.0."""


def sample_moments(returns: Sequence[float]) -> SampleMoments:
    values = [float(value) for value in returns]
    count = len(values)
    if count < 2:
        raise ValueError("sharpe statistics need at least two return observations")

    mean = math.fsum(values) / count
    variance = math.fsum((value - mean) ** 2 for value in values) / (count - 1)
    stdev = math.sqrt(variance) if variance > 0.0 else 0.0
    # A constant series does not reach exactly zero variance in floating point: the mean of
    # three copies of 0.1 is 0.10000000000000002, leaving a residue around 1e-34. Testing
    # `variance > 0` therefore accepts it and returns a Sharpe near 1e17 instead of
    # refusing. The threshold is relative to the magnitude of the data so that it holds for
    # daily returns near 0.01 and for basis points alike.
    magnitude = max(abs(value) for value in values)
    if stdev <= magnitude * 1e-12:
        raise ValueError("return series has no variance; a sharpe ratio is undefined")

    standardised = [(value - mean) / stdev for value in values]
    skewness = math.fsum(item**3 for item in standardised) / count
    kurtosis = math.fsum(item**4 for item in standardised) / count
    return SampleMoments(count, mean, stdev, skewness, kurtosis)


def sharpe_ratio(returns: Sequence[float]) -> float:
    """Per-observation Sharpe. NOT annualised — annualising is a separate, explicit choice."""
    moments = sample_moments(returns)
    return moments.mean / moments.stdev


def _psr_denominator(moments: SampleMoments, observed_sharpe: float) -> float:
    """Variance of the Sharpe estimator under non-normal returns.

    Can go non-positive for extreme skew/kurtosis combinations, at which point the estimator
    itself is meaningless. Refusing beats returning a confident number from a broken formula.
    """
    value = (
        1.0
        - moments.skewness * observed_sharpe
        + ((moments.kurtosis - 1.0) / 4.0) * observed_sharpe**2
    )
    if value <= 0.0:
        raise ValueError(
            "sharpe estimator variance is non-positive for these moments; "
            "the sample is too non-normal for this statistic"
        )
    return value


def probabilistic_sharpe_ratio(returns: Sequence[float], benchmark_sharpe: float = 0.0) -> float:
    """P(true Sharpe > benchmark), corrected for sample length, skew and kurtosis."""
    moments = sample_moments(returns)
    observed = moments.mean / moments.stdev
    denominator = _psr_denominator(moments, observed)
    statistic = (observed - benchmark_sharpe) * math.sqrt(moments.count - 1) / math.sqrt(denominator)
    return _NORMAL.cdf(statistic)


def expected_maximum_sharpe(trials: int, trial_sharpe_variance: float) -> float:
    """The Sharpe the best of ``trials`` genuinely edgeless candidates would be expected to show.

    This is the benchmark a real candidate has to beat. It rises with the number of trials,
    which is the whole point: a sweep of 500 variants has to clear a much higher bar than a
    single pre-registered hypothesis, and pretending otherwise is how backtests lie.
    """
    if not isinstance(trials, int) or isinstance(trials, bool) or trials < 1:
        raise ValueError("trials must be a positive integer")
    if trial_sharpe_variance < 0.0:
        raise ValueError("trial sharpe variance cannot be negative")
    if trials == 1:
        return 0.0
    scale = math.sqrt(trial_sharpe_variance)
    if scale == 0.0:
        return 0.0
    first = _NORMAL.inv_cdf(1.0 - 1.0 / trials)
    second = _NORMAL.inv_cdf(1.0 - 1.0 / (trials * math.e))
    return scale * ((1.0 - _EULER_MASCHERONI) * first + _EULER_MASCHERONI * second)


def deflated_sharpe_ratio(
    returns: Sequence[float],
    *,
    trials: int,
    trial_sharpe_variance: float,
) -> float:
    """P(true Sharpe > the best an edgeless search of this size would have produced).

    Read it as a confidence: 0.95 means a 5% chance this result is selection noise. Below
    about 0.95 a candidate has not cleared its own search.
    """
    benchmark = expected_maximum_sharpe(trials, trial_sharpe_variance)
    return probabilistic_sharpe_ratio(returns, benchmark)


def minimum_track_record_length(
    returns: Sequence[float],
    *,
    benchmark_sharpe: float = 0.0,
    confidence: float = 0.95,
) -> float:
    """Observations needed before this Sharpe could be significant at ``confidence``.

    Returns infinity when the observed Sharpe does not exceed the benchmark at all: no
    amount of further data makes a claim that is not being made.
    """
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence must be strictly between 0 and 1")
    moments = sample_moments(returns)
    observed = moments.mean / moments.stdev
    if observed <= benchmark_sharpe:
        return math.inf
    denominator = _psr_denominator(moments, observed)
    quantile = _NORMAL.inv_cdf(confidence)
    return 1.0 + denominator * (quantile / (observed - benchmark_sharpe)) ** 2
