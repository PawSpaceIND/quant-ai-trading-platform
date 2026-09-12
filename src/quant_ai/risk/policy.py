from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from quant_ai.domain.models import AssetClass, OrderIntent, PortfolioSnapshot


@dataclass(frozen=True)
class RiskPolicy:
    max_daily_loss: Decimal = Decimal("0.02")
    max_drawdown: Decimal = Decimal("0.10")
    max_single_trade_notional: Decimal = Decimal("0.05")
    max_symbol_exposure: Decimal = Decimal("0.10")
    max_asset_class_exposure: Decimal = Decimal("0.40")
    max_gross_exposure: Decimal = Decimal("0.60")
    require_protective_stop: bool = True
    blocked_asset_classes: tuple[AssetClass, ...] = ()


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
        if order.asset_class in self.policy.blocked_asset_classes:
            return RiskDecision(False, "asset_class_blocked")
        if self.policy.require_protective_stop and order.stop_price is None:
            return RiskDecision(False, "protective_stop_required")
        if order.stop_price is not None and order.stop_price <= 0:
            return RiskDecision(False, "invalid_stop_price")
        loss_limit = -(portfolio.equity * self.policy.max_daily_loss)
        if portfolio.daily_realized_pnl <= loss_limit:
            return RiskDecision(False, "daily_loss_limit_reached")
        peak = portfolio.peak_equity or portfolio.equity
        if peak > 0 and (peak - portfolio.equity) / peak >= self.policy.max_drawdown:
            return RiskDecision(False, "max_drawdown_reached")
        notional = order.reference_price * order.quantity
        if notional > portfolio.equity * self.policy.max_single_trade_notional:
            return RiskDecision(False, "single_trade_notional_limit")
        current_symbol = portfolio.symbol_exposure.get(order.symbol, Decimal(0))
        if current_symbol + notional > portfolio.equity * self.policy.max_symbol_exposure:
            return RiskDecision(False, "symbol_concentration_limit")
        current_asset = portfolio.asset_exposure.get(order.asset_class, Decimal(0))
        if current_asset + notional > portfolio.equity * self.policy.max_asset_class_exposure:
            return RiskDecision(False, "asset_class_exposure_limit")
        if portfolio.gross_exposure + notional > portfolio.equity * self.policy.max_gross_exposure:
            return RiskDecision(False, "gross_exposure_limit")
        return RiskDecision(True, "approved")
