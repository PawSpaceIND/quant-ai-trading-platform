from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from quant_ai.agents.atlas import AtlasInvestmentAgent
from quant_ai.agents.contracts import AgentDomain, AgentEvidence, AtlasDecision
from quant_ai.geography.opportunity import CountryOpportunity
from quant_ai.orchestration.cadence import DecisionCadence

REQUIRED_DOMAINS = (
    AgentDomain.TECHNICAL,
    AgentDomain.NEWS,
    AgentDomain.MACRO,
    AgentDomain.COUNTRY,
    AgentDomain.DERIVATIVES,
    AgentDomain.LIQUIDITY,
    AgentDomain.RISK,
    AgentDomain.PORTFOLIO,
)


@dataclass(frozen=True)
class RuntimeStatus:
    ready: bool
    missing_domains: tuple[AgentDomain, ...]
    next_cycle_at: datetime | None
    cycle_count: int


class AtlasRuntimeCoordinator:
    def __init__(
        self,
        atlas: AtlasInvestmentAgent | None = None,
        cadence: DecisionCadence | None = None,
    ) -> None:
        self.atlas = atlas or AtlasInvestmentAgent()
        self.cadence = cadence or DecisionCadence()
        self._history: list[AtlasDecision] = []
        self._last_cycle_at: datetime | None = None

    def status(self, evidence: tuple[AgentEvidence, ...]) -> RuntimeStatus:
        present = {item.domain for item in evidence}
        missing = tuple(domain for domain in REQUIRED_DOMAINS if domain not in present)
        next_cycle = None if self._last_cycle_at is None else self._last_cycle_at + self.cadence.atlas_cycle
        return RuntimeStatus(not missing, missing, next_cycle, len(self._history))

    def due(self, now: datetime) -> bool:
        return self._last_cycle_at is None or now >= self._last_cycle_at + self.cadence.atlas_cycle

    def run_cycle(
        self,
        subject: str,
        evidence: tuple[AgentEvidence, ...],
        now: datetime,
        *,
        country_opportunities: tuple[CountryOpportunity, ...] = (),
        incumbent_country: str = "India",
    ) -> AtlasDecision:
        if not self.due(now):
            raise RuntimeError("atlas_cycle_not_due")
        decision = self.atlas.decide(
            subject,
            evidence,
            now,
            country_opportunities=country_opportunities,
            incumbent_country=incumbent_country,
        )
        self._history.append(decision)
        self._last_cycle_at = now
        return decision

    def recent_decisions(self, limit: int = 10) -> tuple[AtlasDecision, ...]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        return tuple(self._history[-limit:])

    def consensus_stability(self, window: int = 6) -> Decimal:
        decisions = self.recent_decisions(window)
        if len(decisions) < 2:
            return Decimal(1)
        same = sum(1 for left, right in zip(decisions, decisions[1:]) if left.action == right.action)
        return Decimal(same) / Decimal(len(decisions) - 1)
