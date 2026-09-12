from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from quant_ai.agents.contracts import AgentEvidence, AtlasDecision, Stance
from quant_ai.geography.opportunity import CountryOpportunity, expansion_candidates
from quant_ai.governance.founder import FounderPolicy
from quant_ai.llm.anthropic_client import AnthropicSwarmClient
from quant_ai.marketdata.ticker_stream import LiveTick

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
    def __init__(
        self,
        policy: AtlasPolicy | None = None,
        founder_policy: FounderPolicy | None = None,
        llm_client: AnthropicSwarmClient | None = None,
    ) -> None:
        self.policy = policy or AtlasPolicy()
        self.founder_policy = founder_policy or FounderPolicy()
        self.llm_client = llm_client

    def decide(
        self,
        subject: str,
        evidence: tuple[AgentEvidence, ...],
        now: datetime,
        country_opportunities: tuple[CountryOpportunity, ...] = (),
        incumbent_country: str = "India",
        market_tick: LiveTick | None = None,
    ) -> AtlasDecision:
        relevant = tuple(item for item in evidence if item.subject == subject)
        if len(relevant) < self.policy.min_evidence_agents:
            return self._hold(subject, now, relevant, "insufficient_agent_coverage", market_tick)
        stale = tuple(item for item in relevant if item.source_freshness_seconds > self.policy.stale_evidence_seconds)
        if stale:
            return self._hold(subject, now, relevant, "stale_specialist_evidence", market_tick)
        risk_veto = tuple(item for item in relevant if item.stance == Stance.AVOID)
        if risk_veto:
            return self._hold(subject, now, relevant, "specialist_veto", market_tick)

        total_weight = sum((item.confidence for item in relevant), Decimal(0))
        if total_weight <= 0:
            return self._hold(subject, now, relevant, "zero_confidence", market_tick)
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
        ) + _market_rationale(market_tick)
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

    async def decide_with_llm(
        self,
        subject: str,
        evidence: tuple[AgentEvidence, ...],
        now: datetime,
        market_tick: LiveTick | None = None,
    ) -> AtlasDecision:
        deterministic = self.decide(subject, evidence, now, market_tick=market_tick)
        hard_holds = {
            "insufficient_agent_coverage",
            "stale_specialist_evidence",
            "specialist_veto",
            "zero_confidence",
        }
        if self.llm_client is None or any(item in hard_holds for item in deterministic.rationale):
            return deterministic
        payload = await self.llm_client.generate_trading_consensus(
            _atlas_prompt(subject, evidence, market_tick)
        )
        signal, proof = self.llm_client.parse_consensus(payload)
        action = signal.stance
        if signal.expected_risk > self.policy.max_expected_risk:
            action = Stance.NEUTRAL
        supporting = tuple(
            item.agent_id for item in evidence
            if STANCE_SCORE[item.stance] * STANCE_SCORE[action] > 0
        )
        dissenting = tuple(
            item.agent_id for item in evidence
            if STANCE_SCORE[item.stance] * STANCE_SCORE[action] < 0
        )
        rationale = signal.rationale + (
            f"anthropic_model={proof.model}",
            f"xai_summary={proof.summary}",
            *(f"xai_support={item}" for item in proof.supporting_factors),
            *(f"xai_risk={item}" for item in proof.risk_factors),
        ) + _market_rationale(market_tick)
        return AtlasDecision(
            deterministic.cycle_id,
            now,
            action,
            subject,
            signal.confidence,
            signal.expected_return,
            signal.expected_risk,
            supporting,
            dissenting,
            rationale,
            deterministic.country_recommendations,
            deterministic.founder_escalations,
            False,
        )

    def _hold(
        self,
        subject: str,
        now: datetime,
        evidence: tuple[AgentEvidence, ...],
        reason: str,
        market_tick: LiveTick | None = None,
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
            (reason,) + _market_rationale(market_tick),
            (),
            (),
            False,
        )


def _atlas_prompt(
    subject: str, evidence: tuple[AgentEvidence, ...], tick: LiveTick | None
) -> str:
    lines = [
        f"subject={subject}",
        "execution_mode=PAPER_ONLY",
    ]
    for item in evidence:
        lines.append(
            f"agent={item.agent_id};domain={item.domain.value};stance={item.stance.value};"
            f"confidence={item.confidence};expected_return={item.expected_return};"
            f"expected_risk={item.expected_risk};freshness={item.source_freshness_seconds}"
        )
    lines.extend(_market_rationale(tick) if tick is not None else ("live_tick=unavailable",))
    lines.append("Return the structured trading consensus and concise XAI proof.")
    return "\n".join(lines)


def _market_rationale(tick: LiveTick | None) -> tuple[str, ...]:
    if tick is None:
        return ()
    spread = "unknown" if tick.spread is None else str(tick.spread)
    return (
        f"live_ltp={tick.ltp}",
        f"live_volume={tick.volume}",
        f"live_bid_ask_spread={spread}",
        f"live_market_source={tick.source}",
    )
