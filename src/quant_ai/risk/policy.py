from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from quant_ai.domain.models import OrderIntent, PortfolioSnapshot


@dataclass(frozen=True)
class RiskPolicy:
    max_daily_loss: Decimal = Decimal("0.02")
    max_single_trade_notional: Decimal = Decimal("0.05")
    max_gross_exposure: Decimal = Decimal("0.30")


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str


class RiskFirewall:
    def __init__(self, policy: RiskPolicy | None = None) -> None:
        self.policy = policy or RiskPolicy()

    def evaluate(self, order: OrderIntent, portfolio: PortfolioSnapshot) -> RiskDecision:
        if order.quantity <= 0 or order.reference_price <= 0:
            return RiskDecision(False, "invalid_order")

        if portfolio.equity <= 0:
            return RiskDecision(False, "invalid_portfolio_equity")

        loss_limit = -(portfolio.equity * self.policy.max_daily_loss)
        if portfolio.daily_realized_pnl <= loss_limit:
            return RiskDecision(False, "daily_loss_limit_reached")

        notional = order.reference_price * order.quantity
        if notional > portfolio.equity * self.policy.max_single_trade_notional:
            return RiskDecision(False, "single_trade_notional_limit")

        if portfolio.gross_exposure + notional > portfolio.equity * self.policy.max_gross_exposure:
            return RiskDecision(False, "gross_exposure_limit")

        return RiskDecision(True, "approved")
