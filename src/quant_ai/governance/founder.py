from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from quant_ai.agents.contracts import FounderEscalation


@dataclass(frozen=True)
class FounderPolicy:
    max_autonomous_daily_loss: Decimal = Decimal("0.02")
    max_autonomous_capital_change: Decimal = Decimal("0.10")
    max_autonomous_country_allocation: Decimal = Decimal("0.15")
    require_founder_for_live_enablement: bool = True
    require_founder_for_new_jurisdiction: bool = True

    def capital_change_escalation(self, requested_fraction: Decimal) -> FounderEscalation | None:
        if abs(requested_fraction) <= self.max_autonomous_capital_change:
            return None
        return FounderEscalation(
            "CAPITAL_POLICY",
            "requested capital change exceeds autonomous mandate",
            f"Approve capital allocation change of {requested_fraction}",
            "HIGH",
        )

    def jurisdiction_escalation(self, country: str) -> FounderEscalation | None:
        if not self.require_founder_for_new_jurisdiction:
            return None
        return FounderEscalation(
            "NEW_JURISDICTION",
            f"Atlas identified {country} as a candidate market",
            f"Approve research/compliance onboarding for {country}",
            "MEDIUM",
        )
