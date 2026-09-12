from __future__ import annotations

from dataclasses import dataclass

from quant_ai.agents.providers import ProviderStatus, provider_blockers
from quant_ai.config.runtime import RuntimeMode
from quant_ai.integrations.readiness import blockers as integration_blockers
from quant_ai.integrations.readiness import readiness_for_mode

AGENTIC_EXTERNAL_CAPABILITIES = (
    "news_provider",
    "fundamentals_provider",
    "llm_provider",
    "persistent_store",
    "scheduler",
    "notification_channel",
)


@dataclass(frozen=True)
class ClosureReport:
    paper_core_ready: bool
    agentic_paper_ready: bool
    live_ready: bool
    blockers: tuple[str, ...]


def assess_closure(
    *,
    configured_integrations: set[str],
    provider_statuses: tuple[ProviderStatus, ...],
    founder_policy_configured: bool,
    live_execution_enabled: bool = False,
    live_compliance_approved: bool = False,
) -> ClosureReport:
    paper_statuses = readiness_for_mode(RuntimeMode.PAPER, configured_integrations)
    paper_blockers = integration_blockers(paper_statuses)
    source_blockers = provider_blockers(provider_statuses)
    capability_blockers = tuple(
        f"missing_external_capability:{name}"
        for name in AGENTIC_EXTERNAL_CAPABILITIES
        if name not in configured_integrations
    )
    founder_blockers = () if founder_policy_configured else ("founder_policy_not_configured",)
    agentic_blockers = paper_blockers + source_blockers + capability_blockers + founder_blockers

    live_statuses = readiness_for_mode(RuntimeMode.LIVE, configured_integrations)
    live_blockers = list(integration_blockers(live_statuses))
    if not live_compliance_approved:
        live_blockers.append("live_compliance_not_approved")
    if not live_execution_enabled:
        live_blockers.append("live_execution_intentionally_disabled")

    all_blockers = tuple(dict.fromkeys(agentic_blockers + tuple(live_blockers)))
    return ClosureReport(
        paper_core_ready=not paper_blockers,
        agentic_paper_ready=not agentic_blockers,
        live_ready=not live_blockers,
        blockers=all_blockers,
    )
