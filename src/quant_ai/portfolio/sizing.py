from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from quant_ai.domain.models import Opportunity, PortfolioSnapshot, RiskMode


@dataclass(frozen=True)
class SizingPolicy:
    conservative_risk: Decimal = Decimal("0.0025")
    balanced_risk: Decimal = Decimal("0.005")
    aggressive_risk: Decimal = Decimal("0.01")
    max_position_fraction: Decimal = Decimal("0.10")


class PositionSizer:
    def __init__(self, policy: SizingPolicy | None = None) -> None:
        self.policy = policy or SizingPolicy()

    def _risk_fraction(self, mode: RiskMode) -> Decimal:
        return {
            RiskMode.CONSERVATIVE: self.policy.conservative_risk,
            RiskMode.BALANCED: self.policy.balanced_risk,
            RiskMode.AGGRESSIVE: self.policy.aggressive_risk,
        }[mode]

    def quantity(self, opportunity: Opportunity, portfolio: PortfolioSnapshot, price: Decimal, mode: RiskMode) -> int:
        if price <= 0 or portfolio.equity <= 0 or opportunity.stop_distance <= 0:
            return 0
        risk_budget = portfolio.equity * self._risk_fraction(mode)
        per_unit_risk = price * opportunity.stop_distance
        by_risk = (risk_budget / per_unit_risk).to_integral_value(rounding=ROUND_DOWN)
        max_notional = portfolio.equity * self.policy.max_position_fraction
        by_notional = (max_notional / price).to_integral_value(rounding=ROUND_DOWN)
        return max(0, int(min(by_risk, by_notional)))
