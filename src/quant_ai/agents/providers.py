from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from quant_ai.agents.contracts import AgentDomain


@dataclass(frozen=True)
class ProviderSnapshot:
    provider_id: str
    domain: AgentDomain
    subject: str
    observed_at: datetime
    source_timestamp: datetime
    available: bool = True

    def freshness_seconds(self) -> int:
        if self.observed_at.tzinfo is None or self.source_timestamp.tzinfo is None:
            raise ValueError("provider timestamps must be timezone-aware")
        return max(0, int((self.observed_at - self.source_timestamp).total_seconds()))


class SpecialistDataProvider(Protocol):
    provider_id: str
    domain: AgentDomain

    def snapshot(self, subject: str, now: datetime) -> ProviderSnapshot: ...


@dataclass(frozen=True)
class ProviderRequirement:
    domain: AgentDomain
    max_staleness_seconds: int


@dataclass(frozen=True)
class ProviderStatus:
    domain: AgentDomain
    provider_id: str | None
    configured: bool
    fresh: bool
    blocker: str | None


DEFAULT_PROVIDER_REQUIREMENTS = (
    ProviderRequirement(AgentDomain.TECHNICAL, 120),
    ProviderRequirement(AgentDomain.NEWS, 600),
    ProviderRequirement(AgentDomain.MACRO, 3600),
    ProviderRequirement(AgentDomain.COUNTRY, 3600),
    ProviderRequirement(AgentDomain.DERIVATIVES, 300),
    ProviderRequirement(AgentDomain.LIQUIDITY, 120),
    ProviderRequirement(AgentDomain.RISK, 120),
    ProviderRequirement(AgentDomain.PORTFOLIO, 300),
)


def provider_readiness(
    snapshots: tuple[ProviderSnapshot, ...],
    requirements: tuple[ProviderRequirement, ...] = DEFAULT_PROVIDER_REQUIREMENTS,
) -> tuple[ProviderStatus, ...]:
    by_domain = {snapshot.domain: snapshot for snapshot in snapshots}
    statuses: list[ProviderStatus] = []
    for requirement in requirements:
        snapshot = by_domain.get(requirement.domain)
        if snapshot is None:
            statuses.append(
                ProviderStatus(
                    requirement.domain,
                    None,
                    False,
                    False,
                    f"missing_provider:{requirement.domain.value}",
                )
            )
            continue
        if not snapshot.available:
            statuses.append(
                ProviderStatus(
                    requirement.domain,
                    snapshot.provider_id,
                    True,
                    False,
                    f"provider_unavailable:{requirement.domain.value}",
                )
            )
            continue
        fresh = snapshot.freshness_seconds() <= requirement.max_staleness_seconds
        statuses.append(
            ProviderStatus(
                requirement.domain,
                snapshot.provider_id,
                True,
                fresh,
                None if fresh else f"stale_provider:{requirement.domain.value}",
            )
        )
    return tuple(statuses)


def provider_blockers(statuses: tuple[ProviderStatus, ...]) -> tuple[str, ...]:
    return tuple(item.blocker for item in statuses if item.blocker is not None)
