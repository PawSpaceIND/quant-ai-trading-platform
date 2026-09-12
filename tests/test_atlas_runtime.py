from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant_ai.agents.contracts import AgentDomain, AgentEvidence, Stance
from quant_ai.agents.runtime import REQUIRED_DOMAINS, AtlasRuntimeCoordinator


def evidence(domain: AgentDomain, subject: str = "AAPL") -> AgentEvidence:
    return AgentEvidence(
        domain.value.lower(), domain, subject, Stance.BUY, Decimal("0.75"),
        Decimal("0.03"), Decimal("0.02"), ("signal",), datetime.now(timezone.utc), 60,
    )


def full_evidence() -> tuple[AgentEvidence, ...]:
    return tuple(evidence(domain) for domain in REQUIRED_DOMAINS)


def test_runtime_requires_full_specialist_domain_coverage() -> None:
    runtime = AtlasRuntimeCoordinator()
    status = runtime.status((evidence(AgentDomain.TECHNICAL),))
    assert not status.ready
    assert AgentDomain.NEWS in status.missing_domains


def test_runtime_enforces_ten_minute_cycle_and_tracks_history() -> None:
    runtime = AtlasRuntimeCoordinator()
    now = datetime(2026, 9, 12, 9, 0, tzinfo=timezone.utc)
    first = runtime.run_cycle("AAPL", full_evidence(), now)
    assert first.subject == "AAPL"
    with pytest.raises(RuntimeError, match="atlas_cycle_not_due"):
        runtime.run_cycle("AAPL", full_evidence(), now + timedelta(minutes=9))
    second = runtime.run_cycle("AAPL", full_evidence(), now + timedelta(minutes=10))
    assert len(runtime.recent_decisions()) == 2
    assert second.action == first.action
    assert runtime.consensus_stability() == Decimal(1)
