"""Whether a record has run long enough to mean anything, and whether it agrees with research."""
from __future__ import annotations

import math

import pytest

from quant_ai.validation.track_record import (
    MINIMUM_SESSIONS,
    evaluate_track_record,
)


def series(sharpe, count=400, sigma=0.01):
    """Deterministic returns with a per-session Sharpe of about ``sharpe``.

    Alternating one sigma either side of the mean gives exactly that mean and a sample
    deviation within sqrt(n/(n-1)) of sigma. No generator and no seed, so each verdict below
    is a property of the input rather than of a random draw that happened to land well.
    """
    mean = sharpe * sigma
    return [mean + (sigma if index % 2 == 0 else -sigma) for index in range(count)]


def test_no_sessions_means_the_backtest_is_still_an_unchecked_claim():
    verdict = evaluate_track_record([], backtest_sharpe=0.09)

    assert verdict.verdict == "too_short"
    assert not verdict.ready_to_judge
    assert not verdict.supports_going_live
    assert "unchecked claim" in verdict.reasons[0]


def test_a_month_of_trading_concludes_nothing_in_either_direction():
    verdict = evaluate_track_record(series(0.05, count=40), backtest_sharpe=0.09)

    assert verdict.verdict == "too_short"
    assert f"of {MINIMUM_SESSIONS} sessions" in verdict.reasons[0]
    assert "not that it works, not that it is broken" in verdict.reasons[0]


def test_a_record_that_never_beat_zero_is_told_that_more_data_will_not_help():
    verdict = evaluate_track_record(series(-0.02, count=300), backtest_sharpe=0.09)

    assert verdict.verdict == "no_edge"
    assert math.isinf(verdict.minimum_sessions_needed)
    assert "does not exceed zero" in verdict.reasons[0]


def test_a_promising_but_short_record_is_told_how_many_sessions_remain():
    verdict = evaluate_track_record(series(0.04, count=120), backtest_sharpe=0.04)

    assert verdict.verdict == "too_short"
    assert verdict.sessions_remaining > 0
    assert "more sessions" in verdict.reasons[0]
    assert "trading months" in verdict.reasons[0]


def test_a_record_far_below_its_backtest_says_stop_rather_than_wait_for_more_data():
    # The ordering that matters. This record cannot yet establish an edge of its own, so a
    # length-first check would answer "run another few hundred sessions". It has already
    # fallen measurably short of what research claimed, and that is the actionable finding.
    verdict = evaluate_track_record(series(0.02, count=400), backtest_sharpe=0.14)

    assert verdict.verdict == "degraded"
    assert verdict.shortfall_is_significant
    assert not verdict.supports_going_live
    assert "Stop and re-examine rather than waiting" in verdict.reasons[0]
    assert verdict.sessions < verdict.minimum_sessions_needed
    assert any("only conclusion available" in reason for reason in verdict.reasons)


def test_a_long_record_consistent_with_its_research_is_the_only_thing_that_supports_going_live():
    verdict = evaluate_track_record(series(0.12, count=400), backtest_sharpe=0.09)

    assert verdict.verdict == "consistent_with_research"
    assert verdict.ready_to_judge
    assert verdict.supports_going_live
    assert verdict.sessions >= verdict.minimum_sessions_needed


def test_every_other_verdict_refuses_to_support_going_live():
    for returns, claimed in (
        ([], 0.09),
        (series(0.05, count=40), 0.09),
        (series(-0.02, count=300), 0.09),
        (series(0.02, count=400), 0.14),
    ):
        assert not evaluate_track_record(returns, backtest_sharpe=claimed).supports_going_live


def test_the_annualised_figure_scales_the_per_session_sharpe_by_root_252():
    verdict = evaluate_track_record(series(0.12, count=400), backtest_sharpe=0.09)

    assert verdict.annualised_sharpe == pytest.approx(
        verdict.realised_sharpe * math.sqrt(252)
    )


def test_a_backtest_the_record_beats_is_not_treated_as_degradation():
    verdict = evaluate_track_record(series(0.12, count=400), backtest_sharpe=0.01)

    assert verdict.shortfall_against_backtest > 0
    assert not verdict.shortfall_is_significant
    assert verdict.verdict == "consistent_with_research"


def test_evidence_names_what_the_comparison_cannot_tell_you():
    evidence = evaluate_track_record(series(0.12, count=400), backtest_sharpe=0.09).as_evidence()

    assert evidence["schema"] == "pramana.track_record.v1"
    assert "market that changed" in evidence["limitation"]
    assert evidence["supports_going_live"] is True
