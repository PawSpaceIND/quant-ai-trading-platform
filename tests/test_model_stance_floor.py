"""A model BUY under the regime's floor is held like a specialist lean under it.

On 21 September 2026 the consensus model took directional stances at 0.42 confidence
that no specialist lean could have taken, because the floor judged only the specialists.
New risk now needs the plan's conviction whoever proposes it; a SELL is de-risking on a
long-only book and is never held here; and the exploration budget may still turn the
held model BUY into a labelled probe.
"""

from __future__ import annotations

import asyncio

from test_exploration_budget import LEAN, NOW, TREND, budget, consensus_client
from test_regime_playbooks import MODERATE, context

from quant_ai.agents.atlas import AtlasInvestmentAgent
from quant_ai.agents.contracts import Stance


def test_a_model_buy_under_the_regime_floor_is_held_and_says_so() -> None:
    atlas = AtlasInvestmentAgent(llm_client=consensus_client("BUY", 0.62))
    held = asyncio.run(atlas.decide_with_llm("TRENT", MODERATE, NOW, evidence_context=context("trending_down")))
    assert held.action is Stance.NEUTRAL and held.provenance["mode"] == "llm"
    assert "model_confidence_below_floor=0.6200:0.6500" in held.rationale
    assert held.provenance["model_floor"] == {"confidence": "0.6200", "floor": "0.6500", "held": True}
    assert "anthropic_model=m" in held.rationale  # the model's proof is still recorded
    # The same stance clears the trend playbook's plan floor.
    riding = asyncio.run(atlas.decide_with_llm("TRENT", MODERATE, NOW, evidence_context=context("trending_up")))
    assert riding.action is Stance.BUY and "model_floor" not in riding.provenance
    assert not any(item.startswith("model_confidence_below_floor") for item in riding.rationale)


def test_a_model_buy_at_the_plan_floor_is_enough_and_below_it_is_held_without_a_regime() -> None:
    at_floor = AtlasInvestmentAgent(llm_client=consensus_client("BUY", 0.55))
    assert asyncio.run(at_floor.decide_with_llm("TRENT", MODERATE, NOW)).action is Stance.BUY
    weak = AtlasInvestmentAgent(llm_client=consensus_client("BUY", 0.42))
    decision = asyncio.run(weak.decide_with_llm("TRENT", MODERATE, NOW))
    assert decision.action is Stance.NEUTRAL
    assert decision.provenance["model_floor"]["floor"] == "0.5500"


def test_a_model_sell_is_de_risking_and_is_never_held_by_the_floor() -> None:
    atlas = AtlasInvestmentAgent(llm_client=consensus_client("SELL", 0.30))
    decision = asyncio.run(atlas.decide_with_llm("TRENT", MODERATE, NOW, evidence_context=context("high_volatility")))
    assert decision.action is Stance.SELL and "model_floor" not in decision.provenance


def test_a_held_model_buy_over_a_specialist_lean_can_still_become_a_probe() -> None:
    atlas = AtlasInvestmentAgent(policy=budget(3), llm_client=consensus_client("BUY", 0.42))
    decision = asyncio.run(atlas.decide_with_llm("TRENT", LEAN, NOW, evidence_context=TREND))
    assert decision.action is Stance.BUY
    assert decision.provenance["model_floor"]["held"] is True
    assert decision.provenance["exploration"]["probe"] is True and decision.provenance["mode"] == "llm"
    assert decision.rationale[0].startswith("exploration_probe:")
    assert decision.confidence == LEAN[0].confidence  # the probe carries the specialists' average, not the model's
    unbudgeted = AtlasInvestmentAgent(llm_client=consensus_client("BUY", 0.42))
    assert asyncio.run(unbudgeted.decide_with_llm("TRENT", LEAN, NOW, evidence_context=TREND)).action is Stance.NEUTRAL
