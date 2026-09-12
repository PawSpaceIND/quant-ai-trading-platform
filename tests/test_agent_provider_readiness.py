from datetime import datetime, timedelta, timezone

from quant_ai.agents.contracts import AgentDomain
from quant_ai.agents.providers import ProviderSnapshot, provider_blockers, provider_readiness


def snapshot(domain: AgentDomain, freshness_seconds: int = 30, available: bool = True) -> ProviderSnapshot:
    now = datetime(2026, 9, 12, 9, 0, tzinfo=timezone.utc)
    return ProviderSnapshot(
        domain.value.lower(),
        domain,
        "AAPL",
        now,
        now - timedelta(seconds=freshness_seconds),
        available,
    )


def test_missing_and_stale_providers_block_readiness() -> None:
    statuses = provider_readiness((
        snapshot(AgentDomain.TECHNICAL),
        snapshot(AgentDomain.NEWS, 900),
    ))
    blockers = provider_blockers(statuses)
    assert "stale_provider:NEWS" in blockers
    assert "missing_provider:MACRO" in blockers
    assert "missing_provider:RISK" in blockers


def test_all_required_providers_can_be_ready() -> None:
    snapshots = tuple(snapshot(domain) for domain in AgentDomain)
    assert provider_blockers(provider_readiness(snapshots)) == ()
