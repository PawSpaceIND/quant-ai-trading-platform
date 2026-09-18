"""Probability of backtest overfitting, by combinatorially symmetric cross-validation.

The deflated Sharpe asks whether one candidate cleared the bar its search set. This asks a
different and equally important question about the *selection procedure itself*: if I pick
the best-performing candidate in-sample, how often does it land below the median
out-of-sample?

The answer for an honest search is near zero. The answer for an overfit one approaches 0.5
and beyond — at which point "pick the backtest winner" is doing worse than picking at
random, because the thing being selected for is noise that does not repeat.

CSCV works by splitting the observation axis into S chunks and running every balanced
split of those chunks into in-sample and out-of-sample halves. Using all C(S, S/2)
combinations rather than one arbitrary train/test cut is what makes the result symmetric:
no chunk is privileged, and the estimate does not depend on where the single split fell.

Reference: Bailey, Borwein, Lopez de Prado and Zhu (2017), "The Probability of Backtest
Overfitting".
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import combinations

SCHEMA = "pramana.backtest_overfitting.v1"


@dataclass(frozen=True)
class OverfittingReport:
    probability: float
    """Fraction of splits where the in-sample winner ranked below the out-of-sample median."""
    splits: int
    strategies: int
    observations: int
    median_logit: float
    """Median of ln(w/(1-w)). Positive means selection generalised; negative means it did not."""

    @property
    def verdict(self) -> str:
        if self.probability <= 0.10:
            return "selection_generalises"
        if self.probability <= 0.50:
            return "selection_degrades"
        return "selection_is_noise"


def _slice_performance(column: Sequence[float]) -> float:
    """Sharpe of one strategy over one set of observations.

    A slice with no variance has no Sharpe. Rather than crash a whole study, the limit is
    used: zero for a flat-and-zero slice, signed infinity for a flat-but-nonzero one. Both
    sort correctly against real candidates, which is all the ranking needs.
    """
    count = len(column)
    if count < 2:
        raise ValueError("a performance slice needs at least two observations")
    mean = math.fsum(column) / count
    variance = math.fsum((value - mean) ** 2 for value in column) / (count - 1)
    stdev = math.sqrt(variance) if variance > 0.0 else 0.0
    # Same relative threshold as sample_moments: a flat slice never reaches exactly zero
    # variance in floating point, and treating its residue as real produces a Sharpe around
    # 1e17 that wins every in-sample ranking it appears in.
    magnitude = max(abs(value) for value in column)
    if stdev <= magnitude * 1e-12:
        return 0.0 if mean == 0.0 else math.copysign(math.inf, mean)
    return mean / stdev


def probability_of_backtest_overfitting(
    performance: Sequence[Sequence[float]],
    *,
    chunks: int = 8,
) -> OverfittingReport:
    """``performance`` is one row per observation, one column per candidate strategy."""
    if chunks < 2 or chunks % 2 != 0:
        raise ValueError("chunks must be an even number of at least two")
    rows = [tuple(float(value) for value in row) for row in performance]
    observations = len(rows)
    if observations == 0:
        raise ValueError("performance matrix is empty")
    strategies = len(rows[0])
    if strategies < 2:
        raise ValueError("overfitting is a property of a selection, so it needs at least two candidates")
    if any(len(row) != strategies for row in rows):
        raise ValueError("every observation must score the same number of candidates")

    size = observations // chunks
    if size < 2:
        raise ValueError(
            f"{observations} observations cannot be split into {chunks} chunks of at least two"
        )

    # Trailing observations that do not fill a whole chunk are dropped rather than padded
    # or unevenly distributed: equal chunks are what makes the splits symmetric.
    blocks = [tuple(range(index * size, (index + 1) * size)) for index in range(chunks)]

    logits: list[float] = []
    for chosen in combinations(range(chunks), chunks // 2):
        in_sample: list[int] = []
        out_of_sample: list[int] = []
        for index, block in enumerate(blocks):
            (in_sample if index in chosen else out_of_sample).extend(block)

        in_scores = [_slice_performance([rows[i][n] for i in in_sample]) for n in range(strategies)]
        best = max(range(strategies), key=lambda n: in_scores[n])

        out_scores = [_slice_performance([rows[i][n] for i in out_of_sample]) for n in range(strategies)]
        # Rank of the in-sample winner among out-of-sample results: 1 is worst, N is best.
        # Ties count as beaten, which is the conservative direction for an overfitting test.
        rank = 1 + sum(1 for n in range(strategies) if out_scores[n] < out_scores[best])
        relative = rank / (strategies + 1)
        logits.append(math.log(relative / (1.0 - relative)))

    logits.sort()
    middle = len(logits) // 2
    median = (
        logits[middle]
        if len(logits) % 2
        else (logits[middle - 1] + logits[middle]) / 2.0
    )
    degraded = sum(1 for value in logits if value <= 0.0)
    return OverfittingReport(
        probability=degraded / len(logits),
        splits=len(logits),
        strategies=strategies,
        observations=len(blocks) * size,
        median_logit=median,
    )
