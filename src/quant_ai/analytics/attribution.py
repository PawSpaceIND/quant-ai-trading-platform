from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from quant_ai.agents.contracts import AgentEvidence


@dataclass(frozen=True)
class AgentAttribution:
    agent_id: str
    observations: int
    wins: int
    losses: int
    pnl: Decimal
    hit_rate: Decimal
    conviction_weight: Decimal


class AgentAttributionEngine:
    def __init__(self) -> None:
        self._records: dict[str, list[Decimal]] = {}

    def record(self, agent_ids: tuple[str, ...], realized_pnl: Decimal) -> None:
        if not agent_ids:
            return
        share = realized_pnl / Decimal(len(agent_ids))
        for agent_id in agent_ids:
            self._records.setdefault(agent_id, []).append(share)

    def attribution(self) -> tuple[AgentAttribution, ...]:
        rows = []
        for agent_id, pnls in sorted(self._records.items()):
            wins = sum(1 for pnl in pnls if pnl > 0)
            losses = sum(1 for pnl in pnls if pnl < 0)
            observations = len(pnls)
            hit_rate = Decimal(wins) / Decimal(observations) if observations else Decimal("0.5")
            weight = min(Decimal("1.25"), max(Decimal("0.75"), Decimal("0.75") + hit_rate * Decimal("0.5")))
            rows.append(AgentAttribution(agent_id, observations, wins, losses, sum(pnls, Decimal(0)), hit_rate, weight))
        return tuple(rows)

    def weight_evidence(self, evidence: tuple[AgentEvidence, ...]) -> tuple[AgentEvidence, ...]:
        weights = {item.agent_id: item.conviction_weight for item in self.attribution()}
        adjusted = []
        for item in evidence:
            weight = weights.get(item.agent_id, Decimal(1))
            confidence = min(Decimal(1), max(Decimal(0), item.confidence * weight))
            adjusted.append(AgentEvidence(
                item.agent_id, item.domain, item.subject, item.stance, confidence,
                item.expected_return, item.expected_risk,
                item.rationale + (f"attribution_weight={weight}",),
                item.observed_at, item.source_freshness_seconds,
            ))
        return tuple(adjusted)
