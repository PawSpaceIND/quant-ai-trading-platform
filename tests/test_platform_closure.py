from datetime import datetime, timedelta, timezone

from quant_ai.agents.contracts import AgentDomain
from quant_ai.agents.providers import ProviderSnapshot, provider_readiness
from quant_ai.operations.closure import assess_closure


def provider_statuses():
    now = datetime(2026, 9, 12, 9, 0, tzinfo=timezone.utc)
    snapshots = tuple(
        ProviderSnapshot(domain.value.lower(), domain, "AAPL", now, now - timedelta(seconds=30))
        for domain in AgentDomain
    )
    return provider_readiness(snapshots)


def test_agentic_paper_can_be_ready_while_live_remains_disabled() -> None:
    configured = {
        "primary_market_data",
        "paper_broker",
        "news_provider",
        "fundamentals_provider",
        "llm_provider",
        "persistent_store",
        "scheduler",
        "notification_channel",
    }
    report = assess_closure(
        configured_integrations=configured,
        provider_statuses=provider_statuses(),
        founder_policy_configured=True,
    )
    assert report.paper_core_ready
    assert report.agentic_paper_ready
    assert not report.live_ready
    assert "live_execution_intentionally_disabled" in report.blockers


def test_missing_external_capabilities_are_explicit_blockers() -> None:
    report = assess_closure(
        configured_integrations={"paper_broker"},
        provider_statuses=(),
        founder_policy_configured=False,
    )
    assert not report.paper_core_ready
    assert not report.agentic_paper_ready
    assert "missing_required_integration:primary_market_data" in report.blockers
    assert "missing_external_capability:news_provider" in report.blockers
    assert "founder_policy_not_configured" in report.blockers
