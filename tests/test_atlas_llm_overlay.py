"""What the model may and may not do to a decision that has already been made.

Atlas records a probability before the outcome exists, and that number is what the
calibration curve is built from. Until it is calibrated and an explicit EV gate is armed,
the model's job is to talk the book down, never up: it may refuse or soften an entry the
specialists proposed, and it may not propose one they did not, nor restate the probability
under the same basis id.

Two things made that untrue. The overlay took the model's stance outright, so a NEUTRAL
swarm became a BUY on evidence no specialist had weighed; and it rewrote the forecast block
from the model's own stance and confidence, under the same ``weighted_lean_times_confidence.v1``
id, so the curve that basis produces described a mapping that had not run.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

# Sibling test modules by name: pytest puts tests/ on sys.path, the repository root it
# does not (the console script CI runs), so a ``tests.`` package import fails there.
from test_exploration_budget import NOW, consensus_client, evidence
from test_regime_playbooks import MODERATE

from quant_ai.agents import forecast as forecasting
from quant_ai.agents.atlas import AtlasInvestmentAgent
from quant_ai.agents.contracts import AgentDomain, Stance

# Three voters at STRONG_BUY 0.70 and one neutral risk desk: weighted score 1.5, above the
# 1.25 that makes the action STRONG_BUY, at a conviction of 0.70. The one fixture where a
# model can tighten to a stance that still adds risk, which is where the size cap lives.
STRONG = (
    evidence("technical-quant-mas", AgentDomain.TECHNICAL, Stance.STRONG_BUY, "0.70"),
    evidence("geopolitical-analyst", AgentDomain.NEWS, Stance.STRONG_BUY, "0.70"),
    evidence("indian-equities", AgentDomain.COUNTRY, Stance.STRONG_BUY, "0.70"),
    evidence("risk-desk", AgentDomain.RISK, Stance.NEUTRAL, "0.70"),
)


def test_the_v1_mapping_is_pinned_so_a_refit_cannot_land_as_an_edit() -> None:
    """These four numbers are the meaning of ``weighted_lean_times_confidence.v1``.

    A refit ships a new basis id; changing any of these in place would silently move what
    every stored row claimed, and the reliability curve would describe two mappings at once.
    """
    assert forecasting.BASIS == "weighted_lean_times_confidence.v1"
    assert forecasting.probability_up(Decimal(0), Decimal("0.5")) == Decimal("0.5000")
    assert forecasting.probability_up(Decimal("0.6"), Decimal("0.62")) == Decimal("0.5930")
    # Certainty is never claimed, in either direction.
    assert forecasting.probability_up(Decimal(2), Decimal(1)) == Decimal("0.9500")
    assert forecasting.probability_up(Decimal(-2), Decimal(1)) == Decimal("0.0500")


def test_the_model_cannot_rewrite_the_probability_the_decision_is_scored_on() -> None:
    quiet = AtlasInvestmentAgent()
    loud = AtlasInvestmentAgent(llm_client=consensus_client("STRONG_BUY", 0.95))
    deterministic = quiet.decide("TRENT", MODERATE, NOW)
    overlaid = asyncio.run(loud.decide_with_llm("TRENT", MODERATE, NOW))
    assert overlaid.provenance["mode"] == "llm"
    # Same specialists in, same claim recorded - whatever the model said about it.
    assert overlaid.provenance["forecast"] == deterministic.provenance["forecast"]
    assert overlaid.provenance["forecast"]["basis"] == forecasting.BASIS


def test_a_model_that_wants_more_risk_is_recorded_and_not_acted_on() -> None:
    quiet = AtlasInvestmentAgent()
    atlas = AtlasInvestmentAgent(llm_client=consensus_client("STRONG_BUY", 0.95))
    deterministic = quiet.decide("TRENT", MODERATE, NOW)
    decision = asyncio.run(atlas.decide_with_llm("TRENT", MODERATE, NOW))
    # The specialists leaned BUY; the model wanted STRONG_BUY. The lean stands.
    assert decision.action is Stance.BUY
    assert decision.provenance["model_view"]["stance"] == "STRONG_BUY"
    assert decision.provenance["model_view"]["applied"] == "advisory"
    # An action the specialists chose is described by the specialists' own numbers. The
    # risk warden and the sizer read expected_risk, so carrying the model's lower figure
    # under a deterministic action would understate the very thing the action was judged on.
    assert decision.expected_risk == deterministic.expected_risk
    assert decision.expected_return == deterministic.expected_return


def test_a_model_that_wants_less_risk_is_acted_on() -> None:
    atlas = AtlasInvestmentAgent(llm_client=consensus_client("AVOID", 0.80))
    decision = asyncio.run(atlas.decide_with_llm("TRENT", MODERATE, NOW))
    assert decision.action is Stance.AVOID
    assert decision.provenance["model_view"]["applied"] == "tightened"
    # Refusing is always allowed, and the claim recorded for scoring is still the lean's.
    assert decision.provenance["forecast"]["basis"] == forecasting.BASIS


def test_an_entry_is_never_sized_on_conviction_the_specialists_did_not_reach() -> None:
    """The runtime sizes from the decision's confidence, so this is the size guarantee."""
    quiet = AtlasInvestmentAgent()
    loud = AtlasInvestmentAgent(llm_client=consensus_client("STRONG_BUY", 0.95))
    deterministic = quiet.decide("TRENT", MODERATE, NOW)
    overlaid = asyncio.run(loud.decide_with_llm("TRENT", MODERATE, NOW))
    assert overlaid.confidence == deterministic.confidence
    assert overlaid.confidence < Decimal("0.95")


def test_a_tightened_entry_that_still_adds_risk_keeps_the_specialists_conviction() -> None:
    """The one case where the model both tightens and leaves risk on: the size must not rise.

    A STRONG_BUY softened to a BUY is the model doing its job. But the runtime sizes from
    the decision's confidence, so carrying the model's 0.95 through would make a *smaller*
    stance take a *larger* position than the specialists' own 0.70 supported.
    """
    quiet = AtlasInvestmentAgent()
    atlas = AtlasInvestmentAgent(llm_client=consensus_client("BUY", 0.95))
    deterministic = quiet.decide("TRENT", STRONG, NOW)
    decision = asyncio.run(atlas.decide_with_llm("TRENT", STRONG, NOW))
    assert deterministic.action is Stance.STRONG_BUY
    assert decision.action is Stance.BUY
    assert decision.provenance["model_view"]["applied"] == "tightened"
    assert decision.confidence == deterministic.confidence == Decimal("0.70")
