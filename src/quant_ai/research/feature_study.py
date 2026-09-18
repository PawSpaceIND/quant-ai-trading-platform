"""Does a hypothesis predict anything, on history, with the search counted?

`analytics/` already answers the other question: once a decision has been made and the future
has arrived, was the decision good? That is outcome tracking, and it can only grade what the
system already did.

This grades a candidate *before* it is allowed to influence anything. Each feature is
computed causally at every point in history, paired with the return that followed, and scored
out of sample. The best feature is then selected in-sample and measured on data the selection
never saw, because selecting the winner is exactly what manufactures a result out of noise.

Three properties make the answer honest rather than encouraging:

* **Causality.** A feature at t sees `history[:t+1]` and nothing after. The label is the
  return from t to t+horizon, which lies entirely in the future.
* **Purging.** Labels overlap: the observation at t and the one at t+1 share almost all of
  their outcome. Ordinary K-fold leaks that across the boundary, so folds are purged by the
  label span and embargoed after it.
* **The search is counted.** Evaluating a library of N features is N hypotheses. The report
  carries that count for the trial register, and the verdict deflates against it.

Nothing here approves anything, and a good score on survivorship-biased history is still
wrong, because the bias is in the input rather than the estimator.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from quant_ai.features.library import FeatureLibrary
from quant_ai.marketdata.models import Candle
from quant_ai.validation.deflated_sharpe import deflated_sharpe_ratio, sharpe_ratio
from quant_ai.validation.harness import trial_sharpe_variance
from quant_ai.validation.overfitting import probability_of_backtest_overfitting
from quant_ai.validation.purged_cv import purged_kfold

SCHEMA = "pramana.feature_study.v1"

#: Below this many usable observations the study refuses rather than reporting a number.
#: An earlier draft required only four per fold, which let 51 observations produce a
#: deflated Sharpe and a probability of backtest overfitting. Those are real statistics
#: computed on a sample too small to carry them, and a refusal is more useful than a
#: precise-looking answer nobody should act on.
MINIMUM_OBSERVATIONS = 250
MINIMUM_PER_FOLD = 40


@dataclass(frozen=True)
class FeatureScore:
    name: str
    observations: int
    information_coefficient: float
    """Spearman rank correlation between the feature and the return that followed."""
    signal_sharpe: float | None
    """Sharpe of sign(feature) * forward return. None when the series has no dispersion."""


@dataclass(frozen=True)
class FeatureStudy:
    horizon: int
    observations: int
    hypotheses: int
    scores: tuple[FeatureScore, ...]
    selected: str | None
    fold_winners: tuple[str, ...]
    out_of_sample_sharpe: float | None
    deflated_sharpe: float | None
    overfitting_probability: float | None
    clears_statistical_gate: bool
    reasons: tuple[str, ...]

    def as_evidence(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "horizon": self.horizon,
            "observations": self.observations,
            "candidate_trials": self.hypotheses,
            "selected": self.selected,
            "fold_winners": list(self.fold_winners),
            "out_of_sample_sharpe": self.out_of_sample_sharpe,
            "deflated_sharpe": self.deflated_sharpe,
            "overfitting_probability": self.overfitting_probability,
            "clears_statistical_gate": self.clears_statistical_gate,
            "reasons": list(self.reasons),
            "scores": [
                {
                    "name": score.name,
                    "observations": score.observations,
                    "information_coefficient": score.information_coefficient,
                    "signal_sharpe": score.signal_sharpe,
                }
                for score in self.scores
            ],
            "limitation": (
                "Measures whether a hypothesis predicted the next horizon on this history. It "
                "is not a backtest: there is no position sizing, no cost model and no capacity "
                "estimate, and a good score on survivorship-biased input is still wrong."
            ),
        }


def _ranks(values: Sequence[float]) -> list[float]:
    """Average ranks, so ties do not silently order themselves by input position."""
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    position = 0
    while position < len(order):
        end = position
        while end + 1 < len(order) and values[order[end + 1]] == values[order[position]]:
            end += 1
        average = (position + end) / 2.0 + 1.0
        for index in range(position, end + 1):
            ranks[order[index]] = average
        position = end + 1
    return ranks


def rank_correlation(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Spearman. Returns 0.0 when either side is constant and the correlation is undefined."""
    if len(xs) != len(ys):
        raise ValueError("rank correlation needs paired observations")
    if len(xs) < 3:
        raise ValueError("rank correlation needs at least three observations")
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = math.fsum(rx) / len(rx), math.fsum(ry) / len(ry)
    dx = [value - mx for value in rx]
    dy = [value - my for value in ry]
    denominator = math.sqrt(math.fsum(v * v for v in dx) * math.fsum(v * v for v in dy))
    if denominator == 0.0:
        return 0.0
    return math.fsum(a * b for a, b in zip(dx, dy)) / denominator


def _observations(
    history: Sequence[Candle],
    library: FeatureLibrary,
    horizon: int,
) -> tuple[list[dict[str, float | None]], list[float]]:
    """Causal feature values and the return that followed, one row per usable index."""
    rows: list[dict[str, float | None]] = []
    labels: list[float] = []
    longest = max(feature.lookback for feature in library.features)
    for index in range(longest, len(history) - horizon):
        window = history[: index + 1]
        later = history[index + horizon].close
        now = history[index].close
        if now <= 0:
            continue
        values = library.evaluate(window)
        rows.append({
            name: (float(value) if isinstance(value, Decimal) else None)
            for name, value in values.items()
        })
        labels.append(float((later - now) / now))
    return rows, labels


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _position(value: float, centre: float, direction: float) -> float:
    """Long or short, from the feature's distance above or below its centre.

    Taking sign(feature) directly only works for a feature centred on zero. Half this
    library is not: month_position is always positive, range_position lives in [0, 1],
    volatility and turnover are non-negative. For those, sign() is a constant and the
    "signal" degenerates into a permanent long, which in a drifting series scores well while
    using none of the information in the feature. Centring is what makes a feature a signal.

    ``direction`` carries the sign of the relationship learned in-sample, so a feature that
    predicts reversal is traded as reversal rather than counted as a failure.
    """
    return direction * (1.0 if value > centre else -1.0)


def _signal_returns(column: Sequence[float | None], labels: Sequence[float]) -> list[float]:
    """Descriptive only: centred on the whole sample, so it is not an out-of-sample result.

    Used to estimate the SPREAD of candidate Sharpes, which is what the deflation needs. It
    is never the number that gets graded — that comes from the walk-forward path below.
    """
    present = [(value, label) for value, label in zip(column, labels) if value is not None]
    if len(present) < 3:
        return []
    centre = _median([value for value, _ in present])
    # Directed, like the graded path. An undirected version gives every reversal feature a
    # negative Sharpe, which widens the measured spread and therefore raises the bar the
    # winner has to clear - punishing a candidate for the reference distribution's sign.
    correlation = rank_correlation([v for v, _ in present], [label for _, label in present])
    direction = 1.0 if correlation >= 0 else -1.0
    return [_position(value, centre, direction) * label for value, label in present]


def run_feature_study(
    history: Sequence[Candle],
    library: FeatureLibrary,
    *,
    horizon: int = 5,
    splits: int = 5,
    embargo: float = 0.01,
    minimum_deflated_sharpe: float = 0.95,
    maximum_overfitting_probability: float = 0.10,
) -> FeatureStudy:
    if horizon < 1:
        raise ValueError("horizon must be at least one observation")
    rows, labels = _observations(history, library, horizon)
    names = library.names()
    reasons: list[str] = []

    needed = max(MINIMUM_OBSERVATIONS, splits * MINIMUM_PER_FOLD)
    if len(rows) < needed:
        return FeatureStudy(
            horizon, len(rows), library.hypothesis_count, (), None, (), None, None, None, False,
            (
                (
                    f"{len(rows)} usable observations is too few: {needed} are needed before a "
                    f"{library.hypothesis_count}-hypothesis search over {splits} purged folds "
                    "can produce a statistic worth reading"
                ),
            ),
        )

    # Purged and embargoed, because the label at t overlaps the label at t+1 almost entirely.
    folds = purged_kfold(len(rows), splits=splits, label_span=horizon, embargo=embargo)

    scores: list[FeatureScore] = []
    for name in names:
        column = [row[name] for row in rows]
        paired = [(v, labels[i]) for i, v in enumerate(column) if v is not None]
        if len(paired) < 3:
            scores.append(FeatureScore(name, len(paired), 0.0, None))
            continue
        coefficient = rank_correlation([v for v, _ in paired], [label for _, label in paired])
        series = _signal_returns(column, labels)
        try:
            sharpe = sharpe_ratio(series)
        except ValueError:
            sharpe = None
        scores.append(FeatureScore(name, len(paired), coefficient, sharpe))

    # Selection happens inside each fold's training half and is measured on its test half,
    # which is the only arrangement that reveals what selection costs.
    #
    # The out-of-sample series is ONE concatenated path: each fold contributes the test
    # returns of whichever feature won that fold's training. An earlier draft accumulated a
    # separate series per feature and then scored whichever had appeared in most folds,
    # which is not a strategy anybody could have run - it scored a feature on the folds it
    # happened to win and ignored the ones it lost.
    realised: list[float] = []
    winners: list[str] = []
    for fold in folds:
        best, best_score = None, -math.inf
        for name in names:
            train = [(rows[i][name], labels[i]) for i in fold.train if rows[i][name] is not None]
            if len(train) < 3:
                continue
            score = abs(rank_correlation([v for v, _ in train], [label for _, label in train]))
            if score > best_score:
                best, best_score = name, score
        if best is None:
            continue
        winners.append(best)
        # Centre and direction come from the training half only, so the test window never
        # informs the choices applied to it. This is purged cross-validation, not a
        # walk-forward simulation: a fold's training set includes observations after its
        # test block. That is standard for feature evaluation and the purge and embargo
        # handle the label overlap, but it is not a claim that a live system could have
        # traded this path in this order.
        train_values = [rows[i][best] for i in fold.train if rows[i][best] is not None]
        train_labels = [labels[i] for i in fold.train if rows[i][best] is not None]
        centre = _median(train_values)
        direction = 1.0 if rank_correlation(train_values, train_labels) >= 0 else -1.0
        for index in fold.test:
            value = rows[index][best]
            if value is not None:
                realised.append(_position(value, centre, direction) * labels[index])

    # Reported for the reader; the series above is what is graded. Deterministic: most
    # frequent, earliest on a tie. An earlier draft took max() over a set, so when every
    # fold chose a different feature the "selected" name was whichever the set happened to
    # yield first - a label that looked like a finding and was arbitrary.
    chosen = (
        max(winners, key=lambda name: (winners.count(name), -winners.index(name)))
        if winners else None
    )
    if len(realised) < 2:
        reasons.append("no feature was selected often enough to measure out of sample")
        return FeatureStudy(
            horizon, len(rows), library.hypothesis_count, tuple(scores), None, tuple(winners),
            None, None, None, False, tuple(reasons),
        )

    spread = [score.signal_sharpe for score in scores if score.signal_sharpe is not None]
    deflated: float | None = None
    observed: float | None = None
    try:
        observed = sharpe_ratio(realised)
        # A library of one is a pre-registered hypothesis: nothing was selected, so there is
        # no selection bias to measure and no spread to measure it from. That case gets a
        # benchmark of zero, which is the honest easy bar rather than no answer at all.
        # Anything larger must supply the spread of what the search actually produced.
        variance = 0.0 if library.hypothesis_count == 1 else trial_sharpe_variance(spread)
        deflated = deflated_sharpe_ratio(
            realised,
            trials=library.hypothesis_count,
            trial_sharpe_variance=variance,
        )
    except ValueError as error:
        reasons.append(f"selection correction unavailable: {error}")

    overfitting: float | None = None
    centres = {
        name: _median([row[name] for row in rows if row[name] is not None] or [0.0])
        for name in names
    }
    matrix = [
        [
            _position(rows[i][name], centres[name], 1.0) * labels[i]
            if rows[i][name] is not None else 0.0
            for name in names
        ]
        for i in range(len(rows))
    ]
    try:
        overfitting = probability_of_backtest_overfitting(matrix, chunks=8).probability
    except ValueError as error:
        reasons.append(f"overfitting test unavailable: {error}")

    if deflated is None or deflated < minimum_deflated_sharpe:
        reasons.append(
            f"deflated sharpe {deflated if deflated is None else round(deflated, 3)} is below "
            f"{minimum_deflated_sharpe}; a search of {library.hypothesis_count} hypotheses sets "
            "the bar this has to clear"
        )
    if overfitting is None or overfitting > maximum_overfitting_probability:
        reasons.append(
            f"probability of backtest overfitting {overfitting} exceeds "
            f"{maximum_overfitting_probability}"
        )

    return FeatureStudy(
        horizon=horizon,
        observations=len(rows),
        hypotheses=library.hypothesis_count,
        scores=tuple(scores),
        selected=chosen,
        fold_winners=tuple(winners),
        out_of_sample_sharpe=observed,
        deflated_sharpe=deflated,
        overfitting_probability=overfitting,
        clears_statistical_gate=not reasons,
        reasons=tuple(reasons),
    )
