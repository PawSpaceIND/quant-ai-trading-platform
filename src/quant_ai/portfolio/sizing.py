from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from quant_ai.domain.models import Opportunity, PortfolioSnapshot, RiskMode
from quant_ai.planning.capital import CapitalPlan


@dataclass(frozen=True)
class SizingPolicy:
    conservative_risk: Decimal = Decimal("0.0025")
    balanced_risk: Decimal = Decimal("0.005")
    aggressive_risk: Decimal = Decimal("0.01")
    max_position_fraction: Decimal = Decimal("0.10")
    # Mirrors RiskPolicy.max_single_trade_notional so a sized entry clears the firewall
    # instead of being computed at a level the firewall is guaranteed to reject.
    max_trade_fraction: Decimal = Decimal("0.05")


class PositionSizer:
    def __init__(self, policy: SizingPolicy | None = None) -> None:
        self.policy = policy or SizingPolicy()

    def _risk_fraction(self, mode: RiskMode) -> Decimal:
        return {
            RiskMode.CONSERVATIVE: self.policy.conservative_risk,
            RiskMode.BALANCED: self.policy.balanced_risk,
            RiskMode.AGGRESSIVE: self.policy.aggressive_risk,
        }[mode]

    def quantity(
        self,
        opportunity: Opportunity,
        portfolio: PortfolioSnapshot,
        price: Decimal,
        mode: RiskMode,
    ) -> int:
        if price <= 0 or portfolio.equity <= 0 or opportunity.stop_distance <= 0:
            return 0
        risk_budget = portfolio.equity * self._risk_fraction(mode)
        per_unit_risk = price * opportunity.stop_distance
        by_risk = (risk_budget / per_unit_risk).to_integral_value(rounding=ROUND_DOWN)
        max_notional = portfolio.equity * self.policy.max_position_fraction
        by_notional = (max_notional / price).to_integral_value(rounding=ROUND_DOWN)
        return max(0, int(min(by_risk, by_notional)))

    def quantity_from_plan(
        self,
        plan: CapitalPlan,
        portfolio: PortfolioSnapshot,
        price: Decimal,
        *,
        worst_entry_price: Decimal | None = None,
    ) -> int:
        """Size an entry against both intended stop risk and bounded execution friction.

        Four independent caps; the tightest wins. When ``worst_entry_price`` is supplied,
        the risk distance is measured from that adverse fill to the stop derived from the
        signal/reference price. This prevents spread/slippage from silently increasing the
        intended per-trade loss budget or notional/cash usage.
        """
        if price <= 0 or portfolio.equity <= 0 or plan.stop_loss_fraction <= 0:
            return 0
        equity = portfolio.equity
        effective_entry = max(price, worst_entry_price or price)
        stop_price = price * (Decimal(1) - plan.stop_loss_fraction)
        per_unit_risk = effective_entry - stop_price
        if per_unit_risk <= 0:
            return 0
        risk_budget = equity * plan.per_trade_risk_fraction
        by_risk = (risk_budget / per_unit_risk).to_integral_value(rounding=ROUND_DOWN)
        by_position = (
            equity * plan.max_position_fraction / effective_entry
        ).to_integral_value(rounding=ROUND_DOWN)
        by_trade = (
            equity * self.policy.max_trade_fraction / effective_entry
        ).to_integral_value(rounding=ROUND_DOWN)
        deployable = (
            equity * (Decimal(1) - plan.cash_reserve_fraction) - portfolio.gross_exposure
        )
        by_capital = (
            max(Decimal(0), deployable) / effective_entry
        ).to_integral_value(rounding=ROUND_DOWN)
        return max(0, int(min(by_risk, by_position, by_trade, by_capital)))
