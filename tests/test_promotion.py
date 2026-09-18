"""Promotion needs performance AND a corrected search. Either alone approves noise."""

from decimal import Decimal

import pytest

from quant_ai.validation.promotion import (
    PromotionPolicy,
    SelectionEvidence,
    StrategyEvidence,
    evaluate_promotion,
)

STRONG = StrategyEvidence(200, Decimal(10), Decimal("0.05"), Decimal("1.6"), 3, 60)
CORRECTED = SelectionEvidence(candidate_trials=12, deflated_sharpe=0.97, universe_verdict="plausible")


def test_strong_strategy_can_promote_once_its_search_is_accounted_for() -> None:
    assert evaluate_promotion(STRONG, selection=CORRECTED).approved


def test_weak_strategy_is_blocked() -> None:
    evidence = StrategyEvidence(10, Decimal(-1), Decimal("0.20"), Decimal("0.8"), 1, 3)
    decision = evaluate_promotion(evidence, selection=CORRECTED)
    assert not decision.approved
    assert "insufficient_trade_sample" in decision.reasons


def test_perfect_performance_alone_no_longer_promotes() -> None:
    """Every performance threshold cleared. Nobody counted the candidates tried to get here."""
    decision = evaluate_promotion(STRONG)
    assert not decision.approved
    assert decision.reasons == ("selection_bias_uncorrected",)


def test_a_result_that_a_search_this_size_would_have_produced_is_refused() -> None:
    noise = SelectionEvidence(candidate_trials=500, deflated_sharpe=0.42, universe_verdict="plausible")
    decision = evaluate_promotion(STRONG, selection=noise)
    assert not decision.approved
    assert "deflated_sharpe_below_threshold" in decision.reasons


def test_a_survivor_only_universe_is_refused() -> None:
    biased = SelectionEvidence(candidate_trials=12, deflated_sharpe=0.99, universe_verdict="survivor_only")
    decision = evaluate_promotion(STRONG, selection=biased)
    assert not decision.approved
    assert "universe_not_research_grade" in decision.reasons


def test_an_unrecorded_trial_count_is_refused() -> None:
    unrecorded = SelectionEvidence(candidate_trials=0, deflated_sharpe=0.99, universe_verdict="plausible")
    assert "candidate_trials_not_recorded" in evaluate_promotion(STRONG, selection=unrecorded).reasons


def test_selection_is_keyword_only_so_it_cannot_be_passed_where_policy_belongs() -> None:
    with pytest.raises(TypeError):
        evaluate_promotion(STRONG, CORRECTED)  # type: ignore[arg-type]


def test_the_correction_defaults_to_required() -> None:
    assert PromotionPolicy().require_selection_correction is True
    assert PromotionPolicy().min_deflated_sharpe == Decimal("0.95")


def test_disabling_the_correction_is_possible_and_visible_in_the_policy() -> None:
    """Escape hatch for a pre-registered single hypothesis; it has to be stated, not defaulted."""
    relaxed = PromotionPolicy(require_selection_correction=False)
    assert evaluate_promotion(STRONG, relaxed).approved
