"""The harness has to reject noise before it is allowed to endorse anything.

The single most valuable test in this file is ``test_best_of_a_large_sweep_is_rejected``.
It builds two hundred strategies out of pure random numbers, none of which has any edge by
construction, picks the best one exactly as a research sweep would, and confirms the gate
refuses it. A validation harness that passes that candidate is not a weak harness, it is a
harness that will eventually put real money behind noise.
"""

from __future__ import annotations

import math
from random import Random

import pytest

from quant_ai.validation.deflated_sharpe import (
    deflated_sharpe_ratio,
    expected_maximum_sharpe,
    minimum_track_record_length,
    probabilistic_sharpe_ratio,
    sharpe_ratio,
)
from quant_ai.validation.harness import (
    ValidationThresholds,
    trial_sharpe_variance,
    validate_candidate,
)
from quant_ai.validation.overfitting import probability_of_backtest_overfitting
from quant_ai.validation.purged_cv import purged_kfold


def noise(rng: Random, count: int, drift: float = 0.0) -> list[float]:
    return [rng.gauss(drift, 1.0) for _ in range(count)]


def sweep(seed: int, strategies: int, observations: int, drift: float = 0.0) -> list[list[float]]:
    """Columns are strategies, rows are observations."""
    rng = Random(seed)
    columns = [noise(rng, observations, drift) for _ in range(strategies)]
    return [[columns[n][t] for n in range(strategies)] for t in range(observations)]


# --------------------------------------------------------------------------- the noise gate


def test_pure_noise_does_not_clear_the_gate() -> None:
    rng = Random(11)
    candidates = [noise(rng, 500) for _ in range(40)]
    sharpes = [sharpe_ratio(series) for series in candidates]
    best = max(range(len(candidates)), key=lambda n: sharpes[n])

    verdict = validate_candidate(
        candidates[best],
        trials=len(candidates),
        candidate_sharpes=sharpes,
    )
    assert verdict.clears_statistical_gate is False
    assert verdict.reasons


def test_best_of_a_large_sweep_is_rejected() -> None:
    """Two hundred edgeless candidates. The winner looks excellent and must still fail."""
    rng = Random(2026)
    candidates = [noise(rng, 500) for _ in range(200)]
    sharpes = [sharpe_ratio(series) for series in candidates]
    best = max(range(len(candidates)), key=lambda n: sharpes[n])

    # The naive number a backtest would report: a per-observation Sharpe this size
    # annualises to something that would get a strategy funded.
    assert sharpes[best] > 0.09
    assert sharpes[best] * math.sqrt(252) > 1.4

    verdict = validate_candidate(
        candidates[best],
        trials=len(candidates),
        candidate_sharpes=sharpes,
        performance_matrix=[[candidates[n][t] for n in range(200)] for t in range(500)],
    )
    assert verdict.clears_statistical_gate is False
    assert verdict.deflated_sharpe < 0.95
    assert any("deflated sharpe" in reason for reason in verdict.reasons)


def test_selection_over_noise_does_not_generalise() -> None:
    report = probability_of_backtest_overfitting(sweep(5, strategies=20, observations=400))
    assert report.probability > 0.25
    assert report.verdict in {"selection_degrades", "selection_is_noise"}


# ------------------------------------------------------------------- it can still say yes


def test_a_real_edge_with_a_small_search_clears_the_gate() -> None:
    """Otherwise the gate is just `return False`, which proves nothing."""
    rng = Random(7)
    observations = 1500
    winner = noise(rng, observations, drift=0.14)
    others = [noise(rng, observations) for _ in range(9)]
    sharpes = [sharpe_ratio(winner)] + [sharpe_ratio(series) for series in others]
    columns = [winner] + others
    matrix = [[columns[n][t] for n in range(len(columns))] for t in range(observations)]

    verdict = validate_candidate(
        winner,
        trials=len(columns),
        candidate_sharpes=sharpes,
        performance_matrix=matrix,
    )
    assert verdict.clears_statistical_gate is True, verdict.reasons
    assert verdict.deflated_sharpe >= 0.95
    assert verdict.overfitting is not None and verdict.overfitting.probability <= 0.10


def test_the_same_edge_fails_once_the_search_is_large_enough() -> None:
    """The candidate is unchanged. Only the honesty about how hard we looked changes."""
    rng = Random(7)
    observations = 1500
    winner = noise(rng, observations, drift=0.14)
    sharpes = [sharpe_ratio(winner)] + [sharpe_ratio(noise(rng, observations)) for _ in range(9)]

    modest = validate_candidate(winner, trials=10, candidate_sharpes=sharpes,
                                performance_matrix=None)
    exhaustive = validate_candidate(winner, trials=50_000, candidate_sharpes=sharpes,
                                    performance_matrix=None)
    assert modest.deflated_sharpe > exhaustive.deflated_sharpe
    assert exhaustive.clears_statistical_gate is False


# ------------------------------------------------------------------------------ statistics


def test_probabilistic_sharpe_at_its_own_estimate_is_a_coin_flip() -> None:
    series = noise(Random(3), 400, drift=0.05)
    assert probabilistic_sharpe_ratio(series, sharpe_ratio(series)) == pytest.approx(0.5, abs=1e-9)


def test_the_selection_bar_rises_with_the_number_of_trials() -> None:
    bars = [expected_maximum_sharpe(trials, 0.04) for trials in (2, 10, 100, 1000)]
    assert bars == sorted(bars)
    assert all(later > earlier for earlier, later in zip(bars, bars[1:]))


def test_a_single_pre_registered_hypothesis_carries_no_selection_bias() -> None:
    assert expected_maximum_sharpe(1, 0.25) == 0.0


def test_track_record_is_unreachable_when_the_edge_does_not_beat_the_bar() -> None:
    series = noise(Random(4), 300)
    assert minimum_track_record_length(series, benchmark_sharpe=5.0) == math.inf


def test_deflated_sharpe_never_exceeds_the_undeflated_one() -> None:
    series = noise(Random(9), 600, drift=0.1)
    plain = probabilistic_sharpe_ratio(series, 0.0)
    deflated = deflated_sharpe_ratio(series, trials=250, trial_sharpe_variance=0.03)
    assert deflated < plain


# ---------------------------------------------------------------------------- fails closed


def test_trials_has_no_default() -> None:
    with pytest.raises(TypeError):
        validate_candidate([0.1, 0.2, 0.3], candidate_sharpes=[0.1, 0.2])  # type: ignore[call-arg]


def test_a_search_of_one_cannot_estimate_its_own_spread() -> None:
    with pytest.raises(ValueError, match="at least two evaluated candidates"):
        trial_sharpe_variance([0.3])


def test_a_missing_performance_matrix_is_reported_not_ignored() -> None:
    rng = Random(21)
    series = noise(rng, 1500, drift=0.2)
    sharpes = [sharpe_ratio(series), 0.01]
    verdict = validate_candidate(series, trials=2, candidate_sharpes=sharpes)
    assert any("never tested for overfitting" in reason for reason in verdict.reasons)
    assert verdict.clears_statistical_gate is False


def test_degenerate_inputs_refuse() -> None:
    with pytest.raises(ValueError, match="at least two return observations"):
        sharpe_ratio([0.1])
    with pytest.raises(ValueError, match="no variance"):
        sharpe_ratio([0.1, 0.1, 0.1])
    with pytest.raises(ValueError, match="positive integer"):
        expected_maximum_sharpe(0, 0.1)
    with pytest.raises(ValueError, match="even number"):
        probability_of_backtest_overfitting(sweep(1, 4, 100), chunks=7)
    with pytest.raises(ValueError, match="at least two candidates"):
        probability_of_backtest_overfitting([[0.1], [0.2], [0.3], [0.4]])


def test_thresholds_are_explicit_and_tightening_them_can_only_reject_more() -> None:
    rng = Random(31)
    observations = 1500
    winner = noise(rng, observations, drift=0.14)
    sharpes = [sharpe_ratio(winner)] + [sharpe_ratio(noise(rng, observations)) for _ in range(9)]
    strict = ValidationThresholds(minimum_deflated_sharpe=0.999999)
    lenient = validate_candidate(winner, trials=10, candidate_sharpes=sharpes)
    tightened = validate_candidate(
        winner, trials=10, candidate_sharpes=sharpes, thresholds=strict
    )
    assert len(tightened.reasons) >= len(lenient.reasons)


# ------------------------------------------------------------------------------ purged cv


def test_purged_folds_never_share_an_observation() -> None:
    for fold in purged_kfold(200, splits=5, label_span=10, embargo=0.01):
        assert not fold.leaks
        assert fold.test


def test_purging_removes_observations_whose_labels_reach_into_the_test_window() -> None:
    unpurged = purged_kfold(100, splits=4, label_span=0, embargo=0.0)
    purged = purged_kfold(100, splits=4, label_span=5, embargo=0.0)
    assert sum(len(f.train) for f in purged) < sum(len(f.train) for f in unpurged)

    middle = purged[1]
    first_test = middle.test[0]
    # Nothing within the label span before the test window may remain in training.
    assert all(not (first_test - 5 <= index < first_test) for index in middle.train)


def test_the_embargo_drops_observations_after_the_test_window() -> None:
    without = purged_kfold(1000, splits=5, label_span=0, embargo=0.0)[0]
    with_embargo = purged_kfold(1000, splits=5, label_span=0, embargo=0.05)[0]
    assert len(with_embargo.train) == len(without.train) - 50
    assert all(index >= with_embargo.test[-1] + 51 for index in with_embargo.train)


def test_purged_kfold_validates_its_arguments() -> None:
    with pytest.raises(ValueError, match="at least two observations"):
        purged_kfold(1)
    with pytest.raises(ValueError, match="between two and"):
        purged_kfold(10, splits=1)
    with pytest.raises(ValueError, match="embargo must be"):
        purged_kfold(100, embargo=1.0)
