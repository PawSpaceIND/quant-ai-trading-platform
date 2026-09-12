from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from quant_ai.agents.contracts import AgentEvidence


@dataclass(frozen=True)
class AgentHealth:
    total_agents: int
    healthy_agents: int
    stale_agents: tuple[str, ...]
    low_confidence_agents: tuple[str, ...]
    health_score: Decimal


def assess_agent_health(
    evidence: tuple[AgentEvidence, ...],
    *,
    stale_after_seconds: int = 3600,
    min_confidence: Decimal = Decimal("0.40"),
) -> AgentHealth:
    if not evidence:
        return AgentHealth(0, 0, (), (), Decimal(0))
    stale = tuple(sorted(item.agent_id for item in evidence if item.source_freshness_seconds > stale_after_seconds))
    low_confidence = tuple(sorted(item.agent_id for item in evidence if item.confidence < min_confidence))
    unhealthy = set(stale) | set(low_confidence)
    healthy = len({item.agent_id for item in evidence} - unhealthy)
    total = len({item.agent_id for item in evidence})
    return AgentHealth(total, healthy, stale, low_confidence, Decimal(healthy) / Decimal(total))
