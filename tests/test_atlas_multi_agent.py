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


def test_stale_neutral_specialist_does_not_veto_or_dilute_fresh_consensus() -> None:
    now = datetime(2026, 9, 21, 4, 0, tzinfo=timezone.utc)
    items = (
        evidence("technical", AgentDomain.TECHNICAL, Stance.BUY),
        evidence("news", AgentDomain.NEWS, Stance.BUY),
        evidence("risk", AgentDomain.RISK, Stance.BUY),
        evidence("country", AgentDomain.COUNTRY, Stance.BUY),
        AgentEvidence(
            "macro", AgentDomain.MACRO, "AAPL", Stance.NEUTRAL,
            Decimal("0.10"), Decimal(0), Decimal(0),
            ("macro_stale_neutralized",), now, 259200,
        ),
        AgentEvidence(
            "us-equities", AgentDomain.PORTFOLIO, "AAPL", Stance.NEUTRAL,
            Decimal("0.03"), Decimal(0), Decimal(0),
            ("macro_dependent_stale_neutralized",), now, 259200,
        ),
    )
    decision = AtlasInvestmentAgent().decide("AAPL", items, now)
    assert decision.action is Stance.BUY
    assert "stale_specialist_evidence" not in decision.rationale
    assert "abstained_specialists=macro,us-equities" in decision.rationale
    assert "average_confidence=0.75" in decision.rationale


def test_stale_directional_specialist_still_hard_holds() -> None:
    now = datetime(2026, 9, 21, 4, 0, tzinfo=timezone.utc)
    items = (
        evidence("technical", AgentDomain.TECHNICAL, Stance.BUY),
        evidence("news", AgentDomain.NEWS, Stance.BUY),
        evidence("risk", AgentDomain.RISK, Stance.BUY),
        AgentEvidence(
            "macro", AgentDomain.MACRO, "AAPL", Stance.BUY,
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
