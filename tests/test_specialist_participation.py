"""A specialist without applicable evidence must not become a directional voter."""
import asyncio
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from quant_ai.agents.atlas import AtlasInvestmentAgent, AtlasPolicy
from quant_ai.agents.contracts import Stance
from quant_ai.agents.swarm import (
    AgentAnalysisRequest,
    CommodityYieldAgent,
    GeopoliticalAnalystAgent,
    IndianEquitiesAgent,
    TechnicalQuantAgent,
    USEquitiesAgent,
)
from quant_ai.analytics.attribution import AgentAttributionEngine
from quant_ai.domain.models import AssetClass, Market

NOW = datetime(2026, 9, 21, 6, 0, tzinfo=timezone.utc)


def request(*, market=Market.INDIA, asset_class=AssetClass.EQUITY, **metrics):
    return AgentAnalysisRequest("TEST", market, asset_class, NOW, metrics, 0)


@pytest.mark.parametrize("agent,market,asset,reason", [
    (USEquitiesAgent(), Market.INDIA, AssetClass.EQUITY, "non_us_market"),
    (IndianEquitiesAgent(), Market.USA, AssetClass.EQUITY, "non_india_market"),
    (USEquitiesAgent(), Market.USA, AssetClass.METAL, "non_equity_instrument"),
    (IndianEquitiesAgent(), Market.INDIA, AssetClass.COMMODITY, "non_equity_instrument"),
    (TechnicalQuantAgent(), Market.INDIA, AssetClass.EQUITY, "insufficient_price_history"),
])
def test_inapplicable_or_insufficient_evidence_is_a_zero_confidence_abstention(agent, market, asset, reason):
    evidence = agent.analyze(request(market=market, asset_class=asset, price_history_bars=D(49)))
    assert evidence.stance == Stance.NEUTRAL
    assert evidence.confidence == 0
    assert evidence.expected_return == evidence.expected_risk == 0
    assert reason in evidence.rationale
    attribution = AgentAttributionEngine()
    attribution.apply_skill({evidence.agent_id: D("1.25")}, basis="test")
    assert attribution.weight_evidence((evidence,))[0].confidence == 0


def two_directional_voters():
    return (
        GeopoliticalAnalystAgent().analyze(request(news_sentiment=D("0.2"))),
        TechnicalQuantAgent().analyze(request(momentum=D("0.10"), price_history_bars=D(60))),
    )


def test_foreign_specialist_cannot_supply_the_missing_third_vote():
    evidence = (*two_directional_voters(), USEquitiesAgent().analyze(request()))
    decision = AtlasInvestmentAgent().decide("TEST", evidence, NOW)
    assert decision.action == Stance.NEUTRAL
    assert "insufficient_usable_agent_coverage" in decision.rationale


def test_inapplicable_vote_does_not_dilute_three_usable_specialists():
    usable = (
        GeopoliticalAnalystAgent().analyze(request(freshness_multiplier=D("0.75"))),
        CommodityYieldAgent().analyze(request(freshness_multiplier=D("0.5"))),
        TechnicalQuantAgent().analyze(request(momentum=D("0.3"), price_history_bars=D(60))),
    )
    atlas = AtlasInvestmentAgent()
    original = atlas.decide("TEST", usable, NOW)
    assert original.action == Stance.BUY
    with_foreign = atlas.decide("TEST", (*usable, USEquitiesAgent().analyze(request(
        freshness_multiplier=D("0.5")))), NOW)
    assert with_foreign.action == original.action
    assert with_foreign.confidence == original.confidence
    assert with_foreign.expected_return == original.expected_return
    assert "abstained_specialists=us-equities" in with_foreign.rationale


def test_informed_neutral_still_participates_and_preserves_coverage():
    neutral = CommodityYieldAgent().analyze(request())
    assert neutral.stance == Stance.NEUTRAL and neutral.confidence > 0
    decision = AtlasInvestmentAgent().decide("TEST", (*two_directional_voters(), neutral), NOW)
    assert decision.action == Stance.BUY
    assert "abstained_specialists=none" in decision.rationale


def test_short_history_cannot_supply_a_third_vote():
    evidence = (
        GeopoliticalAnalystAgent().analyze(request(news_sentiment=D("0.2"))),
        CommodityYieldAgent().analyze(request(gold_change=D("-0.4"))),
        TechnicalQuantAgent().analyze(request(price_history_bars=D(49))),
    )
    decision = AtlasInvestmentAgent().decide("TEST", evidence, NOW)
    assert decision.action == Stance.NEUTRAL
    assert "insufficient_usable_agent_coverage" in decision.rationale


def test_model_and_exploration_cannot_override_missing_usable_coverage():
    class MustNotCall:
        calls = 0

        async def generate_trading_consensus(self, _prompt):
            self.calls += 1
            raise AssertionError("Missing specialist coverage must stop before the LLM")

    client = MustNotCall()
    # Missing fundamentals already produces zero confidence before the participation fix.
    evidence = (*two_directional_voters(), IndianEquitiesAgent().analyze(request()))
    atlas = AtlasInvestmentAgent(policy=AtlasPolicy(exploration_max_per_day=2), llm_client=client)
    decision = asyncio.run(atlas.decide_with_llm("TEST", evidence, NOW))
    assert client.calls == 0
    assert decision.action == Stance.NEUTRAL
    assert "insufficient_usable_agent_coverage" in decision.rationale
    assert not any("exploration_probe" in reason for reason in decision.rationale)
