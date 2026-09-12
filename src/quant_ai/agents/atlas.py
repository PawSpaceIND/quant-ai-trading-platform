from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from quant_ai.agents.contracts import AgentEvidence, AtlasDecision, Stance
from quant_ai.geography.opportunity import CountryOpportunity, expansion_candidates
from quant_ai.governance.founder import FounderPolicy

STANCE_SCORE = {
    Stance.STRONG_BUY: Decimal(2),
    Stance.BUY: Decimal(1),
    Stance.NEUTRAL: Decimal(0),
    Stance.SELL: Decimal(-1),
    Stance.STRONG_SELL: Decimal(-2),
    Stance.AVOID: Decimal(-3),
}


@dataclass(frozen=True)
class AtlasPolicy:
    min_evidence_agents: int = 4
    min_consensus_confidence: Decimal = Decimal("0.55")
    stale_evidence_seconds: int = 3600
    max_expected_risk: Decimal = Decimal("0.08")


class AtlasInvestmentAgent:
    def __init__(self, policy: AtlasPolicy | None = None, founder_policy: FounderPolicy | None = None) -> None:
        self.policy = policy or AtlasPolicy()
        self.founder_policy = founder_policy or FounderPolicy()

    def decide(
        self,
        subject: str,
        evidence: tuple[AgentEvidence, ...],
        now: datetime,
        country_opportunities: tuple[CountryOpportunity, ...] = (),
        incumbent_country: str = "India",
    ) -> AtlasDecision:
        relevant = tuple(item for item in evidence if item.subject == subject)
        if len(relevant) < self.policy.min_evidence_agents:
            return self._hold(subject, now, relevant, "insufficient_agent_coverage")
        stale = tuple(item for item in relevant if item.source_freshness_seconds > self.policy.stale_evidence_seconds)
        if stale:
            return self._hold(subject, now, relevant, "stale_specialist_evidence")
        risk_veto = tuple(item for item in relevant if item.stance == Stance.AVOID)
        if risk_veto:
            return self._hold(subject, now, relevant, "specialist_veto")

        total_weight = sum((item.confidence for item in relevant), Decimal(0))
        if total_weight <= 0:
            return self._hold(subject, now, relevant, "zero_confidence")
        weighted_score = sum((STANCE_SCORE[item.stance] * item.confidence for item in relevant), Decimal(0)) / total_weight
        confidence = sum((item.confidence for item in relevant), Decimal(0)) / Decimal(len(relevant))
        expected_return = sum((item.expected_return * item.confidence for item in relevant), Decimal(0)) / total_weight
        expected_risk = sum((item.expected_risk * item.confidence for item in relevant), Decimal(0)) / total_weight

        if confidence < self.policy.min_consensus_confidence or expected_risk > self.policy.max_expected_risk:
            action = Stance.NEUTRAL
        elif weighted_score >= Decimal("1.25"):
            action = Stance.STRONG_BUY
        elif weighted_score >= Decimal("0.45"):
            action = Stance.BUY
        elif weighted_score <= Decimal("-1.25"):
            action = Stance.STRONG_SELL
        elif weighted_score <= Decimal("-0.45"):
            action = Stance.SELL
        else:
            action = Stance.NEUTRAL

        supports = tuple(item.agent_id for item in relevant if STANCE_SCORE[item.stance] * STANCE_SCORE[action] > 0)
        dissents = tuple(item.agent_id for item in relevant if STANCE_SCORE[item.stance] * STANCE_SCORE[action] < 0)
        candidates = expansion_candidates(country_opportunities, incumbent_country) if country_opportunities else ()
        recommendations = tuple(item.country for item in candidates[:3])
        escalations = tuple(
            escalation
            for item in candidates[:3]
            if (escalation := self.founder_policy.jurisdiction_escalation(item.country)) is not None
        )
        rationale = (
            f"weighted_consensus={weighted_score}",
            f"average_confidence={confidence}",
            f"expected_return={expected_return}",
            f"expected_risk={expected_risk}",
        )
        return AtlasDecision(
            uuid4().hex,
            now,
            action,
            subject,
            confidence,
            expected_return,
            expected_risk,
            supports,
            dissents,
            rationale,
            recommendations,
            escalations,
            False,
        )

    def _hold(
        self,
        subject: str,
        now: datetime,
        evidence: tuple[AgentEvidence, ...],
        reason: str,
    ) -> AtlasDecision:
        return AtlasDecision(
            uuid4().hex,
            now,
            Stance.NEUTRAL,
            subject,
            Decimal(0),
            Decimal(0),
            Decimal(0),
            (),
            tuple(item.agent_id for item in evidence),
            (reason,),
            (),
            (),
            False,
        )
