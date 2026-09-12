from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from quant_ai.agents.contracts import AgentDomain, AgentEvidence, Stance
from quant_ai.llm.anthropic_client import AnthropicSwarmClient
from quant_ai.marketdata.ticker_stream import LiveTick


@dataclass(frozen=True)
class SpecialistAgent:
    agent_id: str
    domain: AgentDomain
    stale_after_seconds: int = 900
    llm_client: AnthropicSwarmClient | None = None

    def publish(
        self,
        subject: str,
        stance: Stance,
        confidence: Decimal,
        expected_return: Decimal,
        expected_risk: Decimal,
        rationale: tuple[str, ...],
        observed_at: datetime,
        source_freshness_seconds: int,
        market_tick: LiveTick | None = None,
    ) -> AgentEvidence:
        if self.domain is AgentDomain.TECHNICAL and market_tick is not None:
            rationale = rationale + _tick_rationale(market_tick)
        if source_freshness_seconds > self.stale_after_seconds:
            return AgentEvidence(
                self.agent_id,
                self.domain,
                subject,
                Stance.AVOID,
                Decimal(1),
                Decimal(0),
                max(expected_risk, Decimal("0.01")),
                rationale + ("source_data_stale",),
                observed_at,
                source_freshness_seconds,
            )
        return AgentEvidence(
            self.agent_id,
            self.domain,
            subject,
            stance,
            confidence,
            expected_return,
            expected_risk,
            rationale,
            observed_at,
            source_freshness_seconds,
        )

    async def publish_with_llm(
        self,
        subject: str,
        stance: Stance,
        confidence: Decimal,
        expected_return: Decimal,
        expected_risk: Decimal,
        rationale: tuple[str, ...],
        observed_at: datetime,
        source_freshness_seconds: int,
        market_tick: LiveTick | None = None,
    ) -> AgentEvidence:
        base = self.publish(
            subject, stance, confidence, expected_return, expected_risk, rationale,
            observed_at, source_freshness_seconds, market_tick,
        )
        if self.llm_client is None or base.stance is Stance.AVOID:
            return base
        prompt = _specialist_prompt(base, market_tick)
        payload = await self.llm_client.generate_trading_consensus(prompt)
        signal, proof = self.llm_client.parse_consensus(payload)
        return AgentEvidence(
            self.agent_id,
            self.domain,
            subject,
            signal.stance,
            signal.confidence,
            signal.expected_return,
            signal.expected_risk,
            signal.rationale + (f"xai_summary={proof.summary}",),
            observed_at,
            source_freshness_seconds,
        )


def _tick_rationale(tick: LiveTick) -> tuple[str, ...]:
    spread = "unknown" if tick.spread is None else str(tick.spread)
    return (
        f"live_ltp={tick.ltp}",
        f"live_volume={tick.volume}",
        f"live_bid_ask_spread={spread}",
        f"live_market_source={tick.source}",
    )


def _specialist_prompt(evidence: AgentEvidence, tick: LiveTick | None) -> str:
    tick_lines = _tick_rationale(tick) if tick is not None else ("live_tick=unavailable",)
    return "\n".join((
        f"specialist={evidence.agent_id}",
        f"domain={evidence.domain.value}",
        f"subject={evidence.subject}",
        f"deterministic_stance={evidence.stance.value}",
        f"deterministic_confidence={evidence.confidence}",
        *evidence.rationale,
        *tick_lines,
        "Return a conservative structured consensus for paper trading only.",
    ))


DEFAULT_SPECIALISTS = (
    SpecialistAgent("technical-quant", AgentDomain.TECHNICAL),
    SpecialistAgent("news-intelligence", AgentDomain.NEWS, 600),
    SpecialistAgent("macro-rates", AgentDomain.MACRO, 3600),
    SpecialistAgent("country-opportunity", AgentDomain.COUNTRY, 3600),
    SpecialistAgent("derivatives-volatility", AgentDomain.DERIVATIVES, 300),
    SpecialistAgent("liquidity-execution", AgentDomain.LIQUIDITY, 120),
    SpecialistAgent("risk-sentinel", AgentDomain.RISK, 120),
    SpecialistAgent("portfolio-construction", AgentDomain.PORTFOLIO, 300),
)
