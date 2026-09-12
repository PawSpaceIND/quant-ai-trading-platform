from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Plan(str, Enum):
    RESEARCH = "RESEARCH"
    PRO = "PRO"
    INSTITUTIONAL = "INSTITUTIONAL"


@dataclass(frozen=True)
class Entitlements:
    max_strategies: int
    options_analytics: bool
    api_access: bool
    multi_account: bool


PLAN_ENTITLEMENTS = {
    Plan.RESEARCH: Entitlements(5, True, False, False),
    Plan.PRO: Entitlements(50, True, True, False),
    Plan.INSTITUTIONAL: Entitlements(500, True, True, True),
}
