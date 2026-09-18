"""The research loop has to reject noise AND accept a real edge.

A gate that only ever says no is not calibrated, it is broken in a way that looks
conservative. test_a_strong_injected_edge_is_found is the counter-test that keeps the rest
of this file honest.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from random import Random

import pytest

from quant_ai.domain.models import AssetClass, Instrument, Market
from quant_ai.features.library import CORE_LIBRARY, Feature, FeatureLibrary
from quant_ai.marketdata.models import Candle
from quant_ai.research.feature_study import (
    _median,
    _observations,
    _position,
    rank_correlation,
    run_feature_study,
)

INFY = Instrument("INFY", Market.INDIA, AssetClass.EQUITY, "INR", "NSE")
START = datetime(2020, 1, 1, tzinfo=timezone.utc)

RATIONALE = (
    "Participants rebalance on a calendar cycle, so flows concentrate at known dates rather "
    "than arriving uniformly through the period."
)


def series(count: int, *, seed: int = 3, pull: float = 0.0) -> tuple[Candle, ...]:
    """`pull` is a causal mean-reverting force toward the 20-bar average. 0.0 is a random walk."""
    rng = Random(seed)
    closes = [100.0]
    candles: list[Candle] = []
    for index in range(count):
        window = closes[-20:]
        average = sum(window) / len(window)
        gap = (closes[-1] - average) / average
        closes.append(max(closes[-1] * (1 + rng.gauss(0, 0.011) - pull * gap), 1.0))
        close = Decimal(str(round(closes[-1], 4)))
        span = close * Decimal("0.008")
        candles.append(Candle(
            INFY, START + timedelta(days=index),
            open=close, high=close + span, low=close - span, close=close,
            volume=Decimal(rng.randint(500_000, 2_000_000)),
        ))
    return tuple(candles)


# ------------------------------------------------------------------- calibration


def test_a_pure_random_walk_is_rejected() -> None:
    study = run_feature_study(series(1400, pull=0.0), CORE_LIBRARY, horizon=5)
    assert study.clears_statistical_gate is False
    assert study.deflated_sharpe is not None and study.deflated_sharpe < 0.95
    assert study.overfitting_probability is not None and study.overfitting_probability > 0.5


def test_a_strong_injected_edge_is_found() -> None:
    """Without this the gate could be `return False` and every other test would still pass."""
    study = run_feature_study(series(1400, pull=0.20), CORE_LIBRARY, horizon=5)
    assert study.clears_statistical_gate is True, study.reasons
    assert study.deflated_sharpe is not None and study.deflated_sharpe >= 0.95
    assert study.overfitting_probability is not None and study.overfitting_probability <= 0.10
    assert study.out_of_sample_sharpe is not None and study.out_of_sample_sharpe > 0


def test_the_verdict_rises_with_the_strength_of_the_edge() -> None:
    weak = run_feature_study(series(1400, pull=0.05), CORE_LIBRARY, horizon=5)
    strong = run_feature_study(series(1400, pull=0.35), CORE_LIBRARY, horizon=5)
    assert strong.deflated_sharpe > weak.deflated_sharpe
    assert strong.overfitting_probability <= weak.overfitting_probability


def test_selection_scatters_on_noise_and_concentrates_on_a_real_edge() -> None:
    """Which feature wins each fold is itself diagnostic, so the study reports all of them."""
    noise = run_feature_study(series(1400, pull=0.0, seed=11), CORE_LIBRARY, horizon=5)
    edge = run_feature_study(series(1400, pull=0.20), CORE_LIBRARY, horizon=5)
    assert len(set(edge.fold_winners)) < len(set(noise.fold_winners))


# ----------------------------------------------------------------------- causality


def test_a_feature_never_sees_its_own_label() -> None:
    history = series(200)
    rows, labels = _observations(history, CORE_LIBRARY, horizon=5)
    # One row per usable index, and the last label must still have a future to look at.
    longest = max(feature.lookback for feature in CORE_LIBRARY.features)
    assert len(rows) == len(labels) == len(history) - 5 - longest

    # Reconstruct the first label by hand from the raw candles.
    expected = float((history[longest + 5].close - history[longest].close) / history[longest].close)
    assert labels[0] == pytest.approx(expected)


def test_a_feature_computed_at_t_depends_only_on_history_up_to_t() -> None:
    full = series(300)
    truncated = full[:250]
    rows_full, _ = _observations(full, CORE_LIBRARY, horizon=5)
    rows_short, _ = _observations(truncated, CORE_LIBRARY, horizon=5)
    # The rows both series share must be identical: later candles cannot change earlier values.
    shared = min(len(rows_full), len(rows_short))
    assert shared > 50
    assert rows_full[:shared] == rows_short[:shared]


# ------------------------------------------------------- centring and direction


def test_a_strictly_positive_feature_is_not_a_constant_long() -> None:
    """sign(feature) alone degenerates for any feature that never crosses zero.

    Half this library never does: month_position, range_position, volatility, turnover. An
    uncentred signal on those is a permanent long that scores well in a drifting series
    while using none of the information in the feature.
    """
    always_positive = [5.0, 7.0, 9.0, 11.0]
    centre = _median(always_positive)
    positions = [_position(value, centre, 1.0) for value in always_positive]
    assert set(positions) == {-1.0, 1.0}, "a centred feature must be able to take both sides"


def test_direction_is_learned_rather_than_assumed() -> None:
    centre = 0.0
    assert _position(1.0, centre, 1.0) == 1.0
    assert _position(1.0, centre, -1.0) == -1.0, "a reversal feature must be tradeable as reversal"


# ------------------------------------------------------------- rank correlation


def test_rank_correlation_matches_known_values() -> None:
    assert rank_correlation([1, 2, 3, 4, 5], [1, 2, 3, 4, 5]) == pytest.approx(1.0)
    assert rank_correlation([1, 2, 3, 4, 5], [5, 4, 3, 2, 1]) == pytest.approx(-1.0)
    # Monotone but not linear: Spearman sees through the transform, Pearson would not.
    assert rank_correlation([1, 2, 3, 4, 5], [1, 4, 9, 16, 25]) == pytest.approx(1.0)


def test_rank_correlation_averages_ties_rather_than_ordering_by_position() -> None:
    # If ties took their input order, this would not be symmetric.
    forward = rank_correlation([1, 1, 1, 2], [4, 3, 2, 1])
    backward = rank_correlation([1, 1, 1, 2], [2, 3, 4, 1])
    assert forward == pytest.approx(backward)


def test_rank_correlation_of_a_constant_is_zero_not_an_error() -> None:
    assert rank_correlation([5, 5, 5, 5], [1, 2, 3, 4]) == 0.0


def test_rank_correlation_refuses_unusable_input() -> None:
    with pytest.raises(ValueError, match="paired observations"):
        rank_correlation([1, 2, 3], [1, 2])
    with pytest.raises(ValueError, match="at least three"):
        rank_correlation([1, 2], [1, 2])


# ---------------------------------------------------------------------- refusals


def test_too_little_history_reports_why_rather_than_crashing() -> None:
    study = run_feature_study(series(120), CORE_LIBRARY, horizon=5)
    assert study.clears_statistical_gate is False
    assert study.reasons and "too few" in study.reasons[0]
    assert study.scores == ()


def test_an_invalid_horizon_is_refused() -> None:
    with pytest.raises(ValueError, match="horizon must be at least one"):
        run_feature_study(series(300), CORE_LIBRARY, horizon=0)


def test_a_library_of_one_still_reports_its_search_size() -> None:
    single = FeatureLibrary([
        Feature("only_one", "trend", 5, lambda h: (h[-1].close - h[-6].close) / h[-6].close,
                rationale=RATIONALE),
    ])
    study = run_feature_study(series(600), single, horizon=5)
    assert study.hypotheses == 1
    assert study.as_evidence()["candidate_trials"] == 1


def test_the_study_publishes_what_it_is_not() -> None:
    limitation = run_feature_study(series(600), CORE_LIBRARY, horizon=5).as_evidence()["limitation"]
    assert "not a backtest" in limitation
    assert "survivorship" in limitation


def noise_feature(index: int) -> Feature:
    """A feature that cannot win: constant, so its rank correlation is always zero."""
    return Feature(f"inert_{index:02d}", "structure", 1, lambda h: Decimal(index + 1),
                   rationale=RATIONALE)


def reversion_feature() -> Feature:
    """Distance from the 20-bar average - the thing the injected edge actually rewards."""
    def compute(history):
        window = [candle.close for candle in history[-20:]]
        average = sum(window, Decimal(0)) / Decimal(len(window))
        return (history[-1].close - average) / average
    return Feature("distance_from_average", "reversal", 20, compute, rationale=RATIONALE)


def test_a_bigger_search_is_charged_for_even_when_the_winner_is_unchanged() -> None:
    """The property everything else rests on, and the one no other test covered.

    Same data, same winning feature. The only difference is how many hypotheses were on the
    shelf while it was chosen. Twenty inert features cannot win a fold and cannot improve a
    result - but looking at them is still looking, and the bar has to rise.
    """
    history = series(1400, pull=0.20)
    lean = FeatureLibrary([reversion_feature()])
    padded = FeatureLibrary([reversion_feature()] + [noise_feature(i) for i in range(20)])

    small = run_feature_study(history, lean, horizon=5)
    large = run_feature_study(history, padded, horizon=5)

    assert small.selected == large.selected == "distance_from_average"
    assert small.out_of_sample_sharpe == pytest.approx(large.out_of_sample_sharpe)
    assert large.hypotheses == 21 and small.hypotheses == 1
    assert large.deflated_sharpe < small.deflated_sharpe, (
        "adding 20 hypotheses that changed nothing must still raise the bar"
    )


def test_the_folds_are_purged_by_the_label_span_and_embargoed(monkeypatch) -> None:
    """Labels overlap: the observation at t and at t+1 share almost all of their outcome.

    Ordinary K-fold leaks that across the fold boundary and the leak is invisible in the
    result, so this asserts the arguments rather than waiting to notice the symptom.
    """
    from quant_ai.research import feature_study

    captured: dict[str, object] = {}
    original = feature_study.purged_kfold

    def spy(observations, **kwargs):
        captured.update(kwargs)
        return original(observations, **kwargs)

    monkeypatch.setattr(feature_study, "purged_kfold", spy)
    run_feature_study(series(1400), CORE_LIBRARY, horizon=7, splits=4, embargo=0.02)

    assert captured["label_span"] == 7, "folds must be purged by the horizon the label spans"
    assert captured["embargo"] == 0.02
    assert captured["splits"] == 4


# ------------------------------------------------------------------------------- costs


def test_costs_are_real_and_the_net_result_is_lower_than_the_gross_one() -> None:
    """Zero-cost research is the most common way a study lies about a tradeable edge."""
    study = run_feature_study(series(1400, pull=0.20), CORE_LIBRARY, horizon=5)
    assert study.cost is not None
    assert study.cost.round_trip_bps > 1.0, "a round trip that costs nothing is not priced"
    assert study.gross_sharpe is not None and study.out_of_sample_sharpe is not None
    assert study.out_of_sample_sharpe < study.gross_sharpe, (
        "a signal that turns over must pay for turning over"
    )


def test_the_round_trip_is_priced_from_this_instruments_own_liquidity() -> None:
    """Not a constant: a thin name costs more to trade than a liquid one, and should."""
    from quant_ai.research.feature_study import round_trip_cost

    liquid = round_trip_cost(series(400))
    thin = tuple(
        Candle(c.instrument, c.timestamp, open=c.open, high=c.high, low=c.low, close=c.close,
               volume=c.volume / Decimal(500))
        for c in series(400)
    )
    assert round_trip_cost(thin).round_trip_bps > liquid.round_trip_bps * 1.5
    assert liquid.average_daily_volume > 0


def test_a_bigger_ticket_pays_a_smaller_fraction_because_brokerage_is_capped() -> None:
    from quant_ai.research.feature_study import round_trip_cost

    small = round_trip_cost(series(400), trade_notional=Decimal(20_000))
    large = round_trip_cost(series(400), trade_notional=Decimal(500_000))
    assert small.round_trip_bps > large.round_trip_bps


def test_the_candidate_spread_is_measured_net_like_the_winner() -> None:
    """A net result graded against a gross reference distribution is compared to the wrong one.

    Every candidate looks better than it is, the expected maximum rises with them, and the
    winner is refused for failing to clear a bar nobody actually had to clear.
    """
    from quant_ai.research.feature_study import _signal_returns

    column = [float(v) for v in (1, -1, 1, -1, 1, -1, 1, -1, 1, -1)]
    labels = [0.01, -0.01] * 5
    gross = _signal_returns(column, labels, 0.0)
    net = _signal_returns(column, labels, 0.002)
    assert sum(net) < sum(gross), "the descriptive spread must pay the same costs"


def test_a_constant_feature_cannot_be_tested_for_overfitting() -> None:
    """CSCV ranks candidates by performance across splits; a constant column has none.

    Left in, it drags the in-sample winner's relative rank around for reasons that have
    nothing to do with overfitting, which showed up as a probability of exactly 1.0.
    """
    library = FeatureLibrary([reversion_feature()] + [noise_feature(i) for i in range(5)])
    study = run_feature_study(series(1400, pull=0.20), library, horizon=5)
    assert study.overfitting_probability is None
    assert any("more than one position" in reason for reason in study.reasons)


def test_the_cost_model_reaches_the_evidence_record() -> None:
    evidence = run_feature_study(series(1400), CORE_LIBRARY, horizon=5).as_evidence()
    assert evidence["cost"]["source"] == "execution.friction.MarketFrictionModel"
    assert evidence["cost"]["round_trip_bps"] > 0
    assert evidence["gross_sharpe"] is not None


def test_the_study_threads_the_real_cost_into_the_candidate_spread(monkeypatch) -> None:
    """Asserting the call, because testing _signal_returns directly passes either way.

    The unit test above proves the function charges when told to. This proves the study
    tells it to — the sabotage that survived the first pass was the caller quietly passing
    zero while the graded path paid full costs.
    """
    from quant_ai.research import feature_study

    seen: list[float] = []
    original = feature_study._signal_returns

    def spy(column, labels, half_round_trip=0.0):
        seen.append(half_round_trip)
        return original(column, labels, half_round_trip)

    monkeypatch.setattr(feature_study, "_signal_returns", spy)
    study = run_feature_study(series(1400), CORE_LIBRARY, horizon=5)

    assert seen, "_signal_returns was never called"
    assert all(value > 0 for value in seen), (
        "the candidate spread was measured gross while the graded path paid costs"
    )
    assert study.cost is not None
    assert seen[0] == pytest.approx(study.cost.round_trip_fraction / 2.0)
