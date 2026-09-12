from __future__ import annotations

from dataclasses import dataclass

from quant_ai.config.runtime import RuntimeMode
from quant_ai.integrations.contracts import CORE_REQUIREMENTS


@dataclass(frozen=True)
class IntegrationStatus:
    name: str
    configured: bool
    blocker: str | None = None


def readiness_for_mode(mode: RuntimeMode, configured_names: set[str]) -> tuple[IntegrationStatus, ...]:
    statuses: list[IntegrationStatus] = []
    for requirement in CORE_REQUIREMENTS:
        needed = (
            mode in {RuntimeMode.PAPER, RuntimeMode.SHADOW} and requirement.required_for_paper
        ) or (mode == RuntimeMode.LIVE and requirement.required_for_live)
        configured = requirement.name in configured_names
        blocker = None
        if needed and not configured:
            blocker = f"missing_required_integration:{requirement.name}"
        statuses.append(IntegrationStatus(requirement.name, configured, blocker))
    return tuple(statuses)


def blockers(statuses: tuple[IntegrationStatus, ...]) -> tuple[str, ...]:
    return tuple(status.blocker for status in statuses if status.blocker)
