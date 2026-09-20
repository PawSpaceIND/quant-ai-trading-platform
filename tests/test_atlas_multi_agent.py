import json
from datetime import datetime, timezone
from decimal import Decimal

from quant_ai.agents.atlas import AtlasInvestmentAgent
from quant_ai.agents.contracts import AgentDomain, AgentEvidence, Stance
from quant_ai.geography.opportunity import CountryOpportunity


def evidence(agent: str, domain: AgentDomain, stance: Stance, confidence: str = "0.75") -> AgentEvidence:
    return AgentEvidence(
        agent, domain, "AAPL", stance, Decimal(confidence), Decimal("0.04"), Decimal("0.02"),
        ("evidence",), datetime(2026, 9, 12, tzinfo=timezone.utc), 60,
    )


def test_atlas_synthesizes_specialists_and_json() -> None:
    items = (
        evidence("technical", AgentDomain.TECHNICAL, Stance.BUY),
        evidence("news", AgentDomain.NEWS, Stance.BUY),
        evidence("macro", AgentDomain.MACRO, Stance.NEUTRAL),
        evidence("risk", AgentDomain.RISK, Stance.BUY),
    )
    decision = AtlasInvestmentAgent().decide("AAPL", items, datetime(2026, 9, 12, tzinfo=timezone.utc))
    assert decision.action == Stance.BUY
    assert decision.live_execution_allowed is False
    assert json.loads(decision.to_json())["subject"] == "AAPL"


def test_specialist_veto_forces_hold() -> None:
    items = (
        evidence("technical", AgentDomain.TECHNICAL, Stance.STRONG_BUY),
        evidence("news", AgentDomain.NEWS, Stance.BUY),
        evidence("macro", AgentDomain.MACRO, Stance.BUY),
        evidence("risk", AgentDomain.RISK, Stance.AVOID),
    )
    decision = AtlasInvestmentAgent().decide("AAPL", items, datetime(2026, 9, 12, tzinfo=timezone.utc))
    assert decision.action == Stance.NEUTRAL
    assert "specialist_veto" in decision.rationale


def test_country_outperformance_creates_founder_escalation() -> None:
    items = (
        evidence("technical", AgentDomain.TECHNICAL, Stance.BUY),
        evidence("news", AgentDomain.NEWS, Stance.BUY),
        evidence("macro", AgentDomain.MACRO, Stance.BUY),
        evidence("risk", AgentDomain.RISK, Stance.BUY),
    )
    countries = (
        CountryOpportunity("India", Decimal("0.08"), Decimal("0.16"), Decimal("0.9"), Decimal("0.9"), Decimal("0.9")),
        CountryOpportunity("Japan", Decimal("0.14"), Decimal("0.14"), Decimal("0.9"), Decimal("0.9"), Decimal("0.9")),
    )
    decision = AtlasInvestmentAgent().decide(
        "AAPL", items, datetime(2026, 9, 12, tzinfo=timezone.utc), countries, "India"
    )
    assert decision.country_recommendations == ("Japan",)
    assert decision.founder_escalations[0].category == "NEW_JURISDICTION"


def test_three_india_relevant_specialists_clear_the_coverage_floor_and_two_do_not() -> None:
    """Why the floor is three.

    The live roster can field exactly three specialists with a real view on an NSE stock:
    geopolitical, technical and indian-equities. A floor of four was unreachable by
    construction - us-equities is zero for India by design and commodity-yield's daily macro
    is always past the stale rule - and 370 straight NEUTRAL decisions came out of it. Three
    fresh, agreeing, confident specialists must therefore be enough to act; two must not be,
    because that is where a single loud voice starts deciding on its own.
    """
    now = datetime(2026, 9, 12, tzinfo=timezone.utc)
    three = (
        evidence("geopolitical-analyst", AgentDomain.NEWS, Stance.BUY),
        evidence("technical-quant-mas", AgentDomain.TECHNICAL, Stance.BUY),
        evidence("indian-equities", AgentDomain.COUNTRY, Stance.BUY),
    )
    decision = AtlasInvestmentAgent().decide("AAPL", three, now)
    assert "insufficient_agent_coverage" not in decision.rationale
    assert decision.action == Stance.BUY

    two = AtlasInvestmentAgent().decide("AAPL", three[:2], now)
    assert two.action == Stance.NEUTRAL
    assert "insufficient_agent_coverage" in two.rationale


def test_stale_neutral_specialist_does_not_veto_or_dilute_fresh_consensus() -> None:
    # Macro-driven domains are stale past a fortnight (AtlasPolicy.slow_domain_stale_seconds);
    # fifteen days old, these two abstain and neither veto nor dilute the fresh votes.
    now = datetime(2026, 9, 21, 4, 0, tzinfo=timezone.utc)
    items = (
        evidence("technical", AgentDomain.TECHNICAL, Stance.BUY),
        evidence("news", AgentDomain.NEWS, Stance.BUY),
        evidence("risk", AgentDomain.RISK, Stance.BUY),
        evidence("country", AgentDomain.COUNTRY, Stance.BUY),
        AgentEvidence(
            "macro", AgentDomain.MACRO, "AAPL", Stance.NEUTRAL,
            Decimal("0.10"), Decimal(0), Decimal(0),
            ("macro_stale_neutralized",), now, 15 * 86400,
        ),
        AgentEvidence(
            "us-equities", AgentDomain.PORTFOLIO, "AAPL", Stance.NEUTRAL,
            Decimal("0.03"), Decimal(0), Decimal(0),
            ("macro_dependent_stale_neutralized",), now, 15 * 86400,
        ),
    )
    decision = AtlasInvestmentAgent().decide("AAPL", items, now)
    assert decision.action is Stance.BUY
    assert "stale_specialist_evidence" not in decision.rationale
    assert "abstained_specialists=macro,us-equities" in decision.rationale
    assert "average_confidence=0.75" in decision.rationale


def test_stale_directional_specialist_still_hard_holds() -> None:
    # A fast-domain specialist (news, country, technical) two hours old is past its budget.
    # Macro-driven domains have a calendar budget of their own; see test_macro_staleness_budget.
    now = datetime(2026, 9, 21, 4, 0, tzinfo=timezone.utc)
    items = (
        evidence("technical", AgentDomain.TECHNICAL, Stance.BUY),
        evidence("news", AgentDomain.NEWS, Stance.BUY),
        evidence("risk", AgentDomain.RISK, Stance.BUY),
        AgentEvidence(
            "country", AgentDomain.COUNTRY, "AAPL", Stance.BUY,
            Decimal("0.75"), Decimal("0.04"), Decimal("0.02"),
            ("stale_directional",), now, 7200,
        ),
    )
    decision = AtlasInvestmentAgent().decide("AAPL", items, now)
    assert decision.action is Stance.NEUTRAL
    assert "stale_specialist_evidence" in decision.rationale


def test_stale_avoid_specialist_remains_explicit_veto() -> None:
    now = datetime(2026, 9, 21, 4, 0, tzinfo=timezone.utc)
    items = (
        evidence("technical", AgentDomain.TECHNICAL, Stance.BUY),
        evidence("news", AgentDomain.NEWS, Stance.BUY),
        evidence("risk", AgentDomain.RISK, Stance.BUY),
        AgentEvidence(
            "macro", AgentDomain.MACRO, "AAPL", Stance.AVOID,
            Decimal(1), Decimal(0), Decimal("0.02"),
            ("explicit_risk_veto",), now, 259200,
        ),
    )
    decision = AtlasInvestmentAgent().decide("AAPL", items, now)
    assert decision.action is Stance.NEUTRAL
    assert "specialist_veto" in decision.rationale


def test_fresh_neutral_specialist_still_participates_in_consensus_confidence() -> None:
    now = datetime(2026, 9, 21, 4, 0, tzinfo=timezone.utc)
    items = (
        evidence("technical", AgentDomain.TECHNICAL, Stance.BUY),
        evidence("news", AgentDomain.NEWS, Stance.BUY),
        evidence("risk", AgentDomain.RISK, Stance.BUY),
        evidence("macro", AgentDomain.MACRO, Stance.NEUTRAL, confidence="0.75"),
    )
    decision = AtlasInvestmentAgent().decide("AAPL", items, now)
    assert "abstained_specialists=none" in decision.rationale
    assert "average_confidence=0.75" in decision.rationale


def test_stale_abstentions_cannot_reduce_swarm_to_single_agent_decision() -> None:
    now = datetime(2026, 9, 21, 4, 0, tzinfo=timezone.utc)
    items = (
        evidence("technical", AgentDomain.TECHNICAL, Stance.BUY),
        AgentEvidence(
            "news", AgentDomain.NEWS, "AAPL", Stance.NEUTRAL,
            Decimal("0.10"), Decimal(0), Decimal(0),
            ("stale_news",), now, 7200,
        ),
        AgentEvidence(
            "macro", AgentDomain.MACRO, "AAPL", Stance.NEUTRAL,
            Decimal("0.10"), Decimal(0), Decimal(0),
            ("stale_macro",), now, 259200,
        ),
        AgentEvidence(
            "country", AgentDomain.COUNTRY, "AAPL", Stance.NEUTRAL,
            Decimal("0.10"), Decimal(0), Decimal(0),
            ("stale_country",), now, 7200,
        ),
    )
    decision = AtlasInvestmentAgent().decide("AAPL", items, now)
    assert decision.action is Stance.NEUTRAL
    assert "insufficient_usable_agent_coverage" in decision.rationale
