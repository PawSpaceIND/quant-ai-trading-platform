"""Macro is published daily and weekly; both freshness clocks now know it.

The pilot's macro specialist never held a stance in a week of sessions. Not because the
data was missing, once the retired series were replaced, but because two clocks aged it
as if it were a live feed: a four-hour TTL floored its confidence at 0.10 every cycle,
and even a raised TTL would have met Atlas's one-hour hard hold, because the pipeline
ages macro evidence by its oldest series - days. This file pins the two clocks together:
a week of full weight, a fortnight of half weight, silence after a month, and no hard
hold on macro-driven domains inside that fortnight. Fast domains keep the one-hour rule.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from quant_ai.agents.atlas import AtlasInvestmentAgent, AtlasPolicy
from quant_ai.agents.contracts import AgentDomain, AgentEvidence, Stance
from quant_ai.agents.swarm import AgentAnalysisRequest, CommodityYieldAgent
from quant_ai.domain.models import AssetClass, Instrument, Market, PortfolioSnapshot
from quant_ai.intelligence.freshness import DataCategory, FreshnessState, FreshnessValidator
from quant_ai.intelligence.pipeline import SwarmMarketAnalysisPipeline
from quant_ai.intelligence.providers import MACRO_CORE_INDICATORS, MacroSnapshot
from quant_ai.intelligence.sandbox import (
    SandboxFundamentalDataProvider,
    SandboxNewsSentimentProvider,
)
from quant_ai.marketdata.feed import IndiaSandboxMarketDataFeed
from quant_ai.planning.capital import CapitalGoalEngine, CapitalPlanRequest, RiskMode

NOW = datetime(2026, 9, 21, 4, 30, tzinfo=timezone.utc)
D = Decimal
DAY = 24 * 60 * 60
WEEK = 7 * DAY
FORTNIGHT = 14 * DAY


def days(count: int) -> datetime:
    return NOW - timedelta(days=count)


# --------------------------------------------------------------------------------------
# Clock one: the freshness multiplier
# --------------------------------------------------------------------------------------

def test_macro_ttl_is_one_publication_week() -> None:
    assert FreshnessValidator.TTL[DataCategory.MACRO] == WEEK
    # The fast categories are untouched: a live feed and a wire do not get a calendar.
    assert FreshnessValidator.TTL[DataCategory.PRICE] == 60
    assert FreshnessValidator.TTL[DataCategory.NEWS] == 30 * 60
    assert FreshnessValidator.TTL[DataCategory.FUNDAMENTAL] == DAY


def test_macro_weight_fades_over_a_month_instead_of_dying_at_four_hours() -> None:
    validator = FreshnessValidator()
    five = validator.validate(DataCategory.MACRO, days(5), NOW)
    nine = validator.validate(DataCategory.MACRO, days(9), NOW)
    twenty = validator.validate(DataCategory.MACRO, days(20), NOW)
    thirty = validator.validate(DataCategory.MACRO, days(30), NOW)
    assert (five.state, five.confidence_multiplier) == (FreshnessState.FRESH, D(1))
    assert (nine.state, nine.confidence_multiplier) == (FreshnessState.STALE, D("0.50"))
    assert twenty.state is FreshnessState.STALE and D("0.30") < twenty.confidence_multiplier < D("0.40")
    # Past a month the multiplier is at or under the 0.25 line where a stance is forced
    # NEUTRAL, so very old macro can still never hold a view.
    assert thirty.confidence_multiplier <= D("0.25")


# --------------------------------------------------------------------------------------
# Clock two: the Atlas hard hold
# --------------------------------------------------------------------------------------

def vote(agent: str, domain: AgentDomain, stance: Stance, age: int, confidence: str = "0.75") -> AgentEvidence:
    return AgentEvidence(
        agent, domain, "INFY", stance, D(confidence), D("0.04"), D("0.02"), ("evidence",), NOW, age,
    )


FRESH_PAIR = (
    vote("technical-quant-mas", AgentDomain.TECHNICAL, Stance.BUY, 60),
    vote("geopolitical-analyst", AgentDomain.NEWS, Stance.BUY, 60),
)


def test_policy_budgets_are_per_domain() -> None:
    policy = AtlasPolicy()
    assert policy.stale_budget_seconds(AgentDomain.MACRO) == FORTNIGHT
    assert policy.stale_budget_seconds(AgentDomain.PORTFOLIO) == FORTNIGHT
    for fast in (AgentDomain.TECHNICAL, AgentDomain.NEWS, AgentDomain.COUNTRY,
                 AgentDomain.LIQUIDITY, AgentDomain.RISK, AgentDomain.DERIVATIVES):
        assert policy.stale_budget_seconds(fast) == policy.stale_evidence_seconds == 3600


def test_a_nine_day_old_macro_stance_is_a_vote_not_a_hard_hold() -> None:
    macro = vote("commodity-yield", AgentDomain.MACRO, Stance.BUY, 9 * DAY, confidence="0.37")
    decision = AtlasInvestmentAgent().decide("INFY", FRESH_PAIR + (macro,), NOW)
    assert decision.action is Stance.BUY
    assert "stale_specialist_evidence" not in decision.rationale
    assert "commodity-yield" in decision.supporting_agents
    assert "abstained_specialists=none" in decision.rationale


def test_a_fortnight_old_macro_stance_still_hard_holds() -> None:
    macro = vote("commodity-yield", AgentDomain.MACRO, Stance.BUY, FORTNIGHT + 1)
    decision = AtlasInvestmentAgent().decide("INFY", FRESH_PAIR + (macro,), NOW)
    assert decision.action is Stance.NEUTRAL
    assert "stale_specialist_evidence" in decision.rationale


def test_fast_domains_keep_the_one_hour_rule() -> None:
    # The same two-hour age that macro shrugs off is a hard hold for a news view.
    news = vote("geopolitical-analyst", AgentDomain.NEWS, Stance.BUY, 7200)
    technical = vote("technical-quant-mas", AgentDomain.TECHNICAL, Stance.BUY, 60)
    country = vote("indian-equities", AgentDomain.COUNTRY, Stance.BUY, 60)
    decision = AtlasInvestmentAgent().decide("INFY", (news, technical, country), NOW)
    assert decision.action is Stance.NEUTRAL
    assert "stale_specialist_evidence" in decision.rationale


def test_neutral_macro_inside_the_budget_participates_and_outside_it_abstains() -> None:
    inside = vote("commodity-yield", AgentDomain.MACRO, Stance.NEUTRAL, 9 * DAY, confidence="0.37")
    outside = vote("commodity-yield", AgentDomain.MACRO, Stance.NEUTRAL, FORTNIGHT + 1, confidence="0.37")
    country = vote("indian-equities", AgentDomain.COUNTRY, Stance.BUY, 60)
    atlas = AtlasInvestmentAgent()
    with_inside = atlas.decide("INFY", FRESH_PAIR + (country, inside), NOW)
    with_outside = atlas.decide("INFY", FRESH_PAIR + (country, outside), NOW)
    # Inside the budget a neutral macro read is a participant: it is counted and it
    # weighs on the average confidence, exactly as a fresh neutral news read would.
    assert "abstained_specialists=none" in with_inside.rationale
    assert with_inside.confidence < with_outside.confidence
    # Outside it the read abstains, and the three fresh votes decide alone.
    assert "abstained_specialists=commodity-yield" in with_outside.rationale
    assert with_outside.action is Stance.BUY
    # Two fresh votes plus an abstention is still two votes.
    short = atlas.decide("INFY", FRESH_PAIR + (outside,), NOW)
    assert "insufficient_usable_agent_coverage" in short.rationale


# --------------------------------------------------------------------------------------
# The two clocks together, through the real specialist and the real pipeline
# --------------------------------------------------------------------------------------

def test_commodity_specialist_at_half_weight_holds_a_stance() -> None:
    request = AgentAnalysisRequest(
        "INFY", Market.INDIA, AssetClass.EQUITY, NOW,
        {"brent_change": D("0.10"), "gold_change": D("0.05"), "yield_change": D("0.05"),
         "usd_broad_change": D("0.04"), "freshness_multiplier": D("0.50")},
        9 * DAY,
    )
    evidence = CommodityYieldAgent().analyze(request)
    assert evidence.stance in {Stance.SELL, Stance.STRONG_SELL}
    assert evidence.confidence == D("0.37")
    assert evidence.source_freshness_seconds == 9 * DAY


class NineDayMacro:
    """A macro provider whose newest series is a day old and whose oldest is nine."""

    def fetch(self, indicators, now):
        values = {name: D("100") for name in indicators}
        return MacroSnapshot(values, now - timedelta(days=1), oldest_observed_at=now - timedelta(days=9))


def test_pipeline_lets_a_nine_day_old_macro_read_participate() -> None:
    pipeline = SwarmMarketAnalysisPipeline(
        IndiaSandboxMarketDataFeed(), SandboxNewsSentimentProvider(),
        SandboxFundamentalDataProvider(), NineDayMacro(),
    )
    plan = CapitalGoalEngine().recommend(CapitalPlanRequest(
        D(100000), D("0.8"), D("0.2"), expected_edge=D("0.02"), requested_mode=RiskMode.BALANCED,
    ))
    result = pipeline.run(
        Instrument("INFY", Market.INDIA, AssetClass.EQUITY, "INR", "NSE"), NOW, plan,
        PortfolioSnapshot(D(100000), D(0), D(0)), country="India",
    )
    assert result.freshness.macro.state is FreshnessState.STALE
    assert result.freshness.macro.confidence_multiplier == D("0.50")
    macro = next(item for item in result.evidence if item.agent_id == "commodity-yield")
    # Half weight, a nine-day age, and counted: not silenced, not abstained.
    assert macro.confidence == D("0.37")
    assert macro.source_freshness_seconds == 9 * DAY
    # The read reached the specialists and the journal as the change metrics it becomes.
    assert {"us10y", "yield_change", "brent_change", "gold_change", "usd_broad_change"} <= set(result.features)
    assert len(MACRO_CORE_INDICATORS) == 4
    rationale = result.execution.xai_trace.decision.rationale if hasattr(result.execution.xai_trace, "decision") else ()
    assert "stale_specialist_evidence" not in rationale
