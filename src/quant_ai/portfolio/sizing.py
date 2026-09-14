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

    def quantity(self, opportunity: Opportunity, portfolio: PortfolioSnapshot, price: Decimal, mode: RiskMode) -> int:
        if price <= 0 or portfolio.equity <= 0 or opportunity.stop_distance <= 0:
            return 0
        risk_budget = portfolio.equity * self._risk_fraction(mode)
        per_unit_risk = price * opportunity.stop_distance
        by_risk = (risk_budget / per_unit_risk).to_integral_value(rounding=ROUND_DOWN)
        max_notional = portfolio.equity * self.policy.max_position_fraction
        by_notional = (max_notional / price).to_integral_value(rounding=ROUND_DOWN)
        return max(0, int(min(by_risk, by_notional)))

    def quantity_from_plan(
        self, plan: CapitalPlan, portfolio: PortfolioSnapshot, price: Decimal
    ) -> int:
        """Size an entry from the *live* portfolio and the capital plan's risk parameters.

        Four independent caps; the tightest wins:
        - risk budget: equity x per-trade risk fraction, spent at the plan's stop distance
        - position cap: equity x the plan's max position fraction
        - trade cap: equity x max_trade_fraction (the firewall's single-trade limit)
        - deployable capital: equity net of the plan's cash reserve, less gross exposure
          already deployed - so the size shrinks as capital is committed elsewhere
        Uses current equity rather than the plan's starting capital, so sizing follows the
        account up and down instead of staying pinned to day-one numbers.
        """
        if price <= 0 or portfolio.equity <= 0 or plan.stop_loss_fraction <= 0:
            return 0
        equity = portfolio.equity
        risk_budget = equity * plan.per_trade_risk_fraction
        per_unit_risk = price * plan.stop_loss_fraction
        by_risk = (risk_budget / per_unit_risk).to_integral_value(rounding=ROUND_DOWN)
        by_position = (equity * plan.max_position_fraction / price).to_integral_value(
            rounding=ROUND_DOWN
        )
        by_trade = (equity * self.policy.max_trade_fraction / price).to_integral_value(
            rounding=ROUND_DOWN
        )
        deployable = equity * (Decimal(1) - plan.cash_reserve_fraction) - portfolio.gross_exposure
        by_capital = (max(Decimal(0), deployable) / price).to_integral_value(rounding=ROUND_DOWN)
        return max(0, int(min(by_risk, by_position, by_trade, by_capital)))
