"""A catalogue of hypotheses is only useful if it refuses bad hypotheses and bad data.

The test that carries the most weight is test_evaluating_the_whole_library_is_a_search_of
_that_size: it connects the library to the promotion gate, so adding features raises the bar
a candidate has to clear instead of quietly making it easier to find something.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from random import Random

import pytest

from quant_ai.domain.models import AssetClass, Instrument, Market
from quant_ai.features.library import (
    CORE_LIBRARY,
    FAMILIES,
    Feature,
    FeatureLibrary,
    _momentum,
)
from quant_ai.marketdata.models import Candle

INFY = Instrument("INFY", Market.INDIA, AssetClass.EQUITY, "INR", "NSE")
START = datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc)

REAL_RATIONALE = (
    "Participants rebalance on a calendar cycle, so flows concentrate at known dates rather "
    "than arriving uniformly through the period."
)


def series(count: int, *, seed: int = 5, flat: bool = False) -> tuple[Candle, ...]:
    rng = Random(seed)
    candles: list[Candle] = []
    close = Decimal(1000)
    for index in range(count):
        if not flat:
            close = close * (Decimal(1) + Decimal(str(round(rng.gauss(0, 0.01), 6))))
            close = max(close, Decimal(1))
        span = Decimal(0) if flat else close * Decimal("0.01")
        candles.append(Candle(
            INFY, START + timedelta(days=index),
            open=close, high=close + span, low=close - span, close=close,
            volume=Decimal(1000 if flat else rng.randint(500, 5000)),
        ))
    return tuple(candles)


# ------------------------------------------------------------------ the catalogue works


def test_every_feature_computes_on_a_long_enough_history() -> None:
    history = series(200)
    values = CORE_LIBRARY.evaluate(history)
    assert set(values) == set(CORE_LIBRARY.names())
    uncomputed = sorted(name for name, value in values.items() if value is None)
    assert uncomputed == [], f"features returned None on 200 sessions: {uncomputed}"
    assert all(isinstance(value, Decimal) for value in values.values())


def test_the_catalogue_covers_every_family() -> None:
    for family in FAMILIES:
        assert CORE_LIBRARY.by_family(family), f"no features in family {family}"
    assert len(CORE_LIBRARY) == sum(len(CORE_LIBRARY.by_family(f)) for f in FAMILIES)


# ---------------------------------------------------- missing data is None, never zero


def test_short_history_returns_none_rather_than_a_fabricated_zero() -> None:
    """technical.py returns Decimal(0) here, which downstream code consumes as a real value.

    Zero volatility and zero return are meaningful numbers. Reporting them for "I could not
    compute this" injects a measurement nobody took.
    """
    values = CORE_LIBRARY.evaluate(series(3))
    long_horizon = [name for name in ("momentum_63", "realised_volatility_63", "turnover_21")]
    for name in long_horizon:
        assert values[name] is None, f"{name} produced a value from three sessions"
        assert values[name] != Decimal(0)


def test_a_degenerate_window_returns_none_rather_than_dividing_by_zero() -> None:
    flat = series(120, flat=True)
    values = CORE_LIBRARY.evaluate(flat)
    # A flat series has no range, no dispersion and no volume variation. Those features are
    # unmeasurable here and say so; the ones that remain well defined still compute.
    assert values["range_position_21"] is None
    assert values["volume_zscore_21"] is None
    assert values["body_share"] is None
    assert values["momentum_21"] == Decimal(0), "a genuinely zero return is still a value"


def test_an_empty_history_computes_nothing_and_raises_nothing() -> None:
    assert set(CORE_LIBRARY.evaluate(()).values()) == {None}


# --------------------------------------------------------- a rationale is an argument


def test_a_rationale_that_explains_the_feature_by_its_results_is_refused() -> None:
    for excuse in (
        "This feature had the best Sharpe of everything we tried on the sample we had.",
        "Chosen because it outperformed the alternatives across the whole backtest window.",
        "It was profitable in-sample and we kept it for that reason alone, nothing more.",
    ):
        with pytest.raises(ValueError, match="explains the feature by its results"):
            Feature("candidate_x", "trend", 5, _momentum(4), rationale=excuse)


def test_a_rationale_too_short_to_be_an_argument_is_refused() -> None:
    with pytest.raises(ValueError, match="real economic argument"):
        Feature("candidate_x", "trend", 5, _momentum(4), rationale="momentum works")


def test_every_shipped_rationale_survives_its_own_rule() -> None:
    for feature in CORE_LIBRARY.features:
        assert len(feature.rationale.strip()) >= 40
        # Reconstructing each one re-runs the constructor's checks over the shipped text.
        Feature(feature.name, feature.family, feature.lookback, feature.compute,
                rationale=feature.rationale)


def test_structural_mistakes_are_refused() -> None:
    with pytest.raises(ValueError, match="unknown family"):
        Feature("candidate_x", "vibes", 5, _momentum(4), rationale=REAL_RATIONALE)
    with pytest.raises(ValueError, match="lookback must be positive"):
        Feature("candidate_x", "trend", 0, _momentum(4), rationale=REAL_RATIONALE)
    with pytest.raises(ValueError, match="lowercase identifier"):
        Feature("Candidate X", "trend", 5, _momentum(4), rationale=REAL_RATIONALE)
    with pytest.raises(ValueError, match="duplicate feature names"):
        FeatureLibrary([
            Feature("twin", "trend", 5, _momentum(4), rationale=REAL_RATIONALE),
            Feature("twin", "reversal", 5, _momentum(4), rationale=REAL_RATIONALE),
        ])
    with pytest.raises(ValueError, match="at least one feature"):
        FeatureLibrary([])


# ------------------------------------------------- the library pays for its own size


def test_evaluating_the_whole_library_is_a_search_of_that_size() -> None:
    """Adding features raises the bar. It must never lower it.

    A library of 24 features tested against one target is 24 hypotheses, and the promotion
    gate charges for all of them. Without this link, growing the catalogue would quietly
    make it easier to find something that looks good.
    """
    from quant_ai.validation.deflated_sharpe import expected_maximum_sharpe
    from quant_ai.validation.promotion import (
        PromotionPolicy,
        SelectionEvidence,
        StrategyEvidence,
        evaluate_promotion,
    )

    assert CORE_LIBRARY.hypothesis_count == len(CORE_LIBRARY.features)

    spread = 0.04
    one_hypothesis = expected_maximum_sharpe(1, spread)
    whole_library = expected_maximum_sharpe(CORE_LIBRARY.hypothesis_count, spread)
    assert whole_library > one_hypothesis, "searching 24 ideas must cost more than pre-registering 1"

    # A result that would pass as a single pre-registered idea fails once the search is counted.
    strong = StrategyEvidence(200, Decimal(10), Decimal("0.05"), Decimal("1.6"), 3, 60)
    searched = SelectionEvidence(
        candidate_trials=CORE_LIBRARY.hypothesis_count,
        deflated_sharpe=0.61,
        universe_verdict="plausible",
    )
    decision = evaluate_promotion(strong, PromotionPolicy(), selection=searched)
    assert not decision.approved
    assert "deflated_sharpe_below_threshold" in decision.reasons


def test_the_library_publishes_what_it_is_and_is_not() -> None:
    evidence = CORE_LIBRARY.as_evidence()
    assert evidence["features"] == len(CORE_LIBRARY)
    assert sum(evidence["families"].values()) == len(CORE_LIBRARY)
    assert "not of edges" in evidence["limitation"]
