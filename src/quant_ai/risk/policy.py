from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from quant_ai.domain.models import AssetClass, OrderIntent, PortfolioSnapshot, Side


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
        if order.stop_price is not None and order.stop_price <= 0:
            return RiskDecision(False, "invalid_stop_price")
        if order.take_profit_price is not None and order.take_profit_price <= 0:
            return RiskDecision(False, "invalid_take_profit_price")

        notional = order.reference_price * order.quantity
        current_symbol = portfolio.symbol_exposure.get(order.symbol, Decimal(0))
        # Directionality. On this long-only ledger a SELL unwinds exposure up to the amount
        # held; only the slice beyond the holding would *add* (short) exposure and is
        # therefore the only slice the caps apply to. A BUY adds all of its notional.
        reducing, adding = self._exposure_delta(order, portfolio, notional, current_symbol)

        if adding == 0:
            # Pure de-risking. Neither the concentration caps nor the loss/drawdown halts
            # may block it: a halt freezes risk-taking, never the ability to cut risk.
            return RiskDecision(True, "approved_risk_reducing")

        if self.policy.require_protective_stop and order.stop_price is None:
            return RiskDecision(False, "protective_stop_required")
        # Use the more conservative loss signal. This preserves a realized loss even when
        # no daily equity baseline exists yet, while also catching unrealized MTM losses.
        intraday_pnl = min(portfolio.daily_realized_pnl, portfolio.daily_total_pnl)
        loss_limit = -(portfolio.equity * self.policy.max_daily_loss)
        if intraday_pnl <= loss_limit:
            return RiskDecision(False, "daily_loss_limit_reached")
        peak = portfolio.peak_equity or portfolio.equity
        if peak > 0 and (peak - portfolio.equity) / peak >= self.policy.max_drawdown:
            return RiskDecision(False, "max_drawdown_reached")
        if adding > portfolio.equity * self.policy.max_single_trade_notional:
            return RiskDecision(False, "single_trade_notional_limit")
        projected_symbol = current_symbol - reducing + adding
        if projected_symbol > portfolio.equity * self.policy.max_symbol_exposure:
            return RiskDecision(False, "symbol_concentration_limit")
        current_asset = portfolio.asset_exposure.get(order.asset_class, Decimal(0))
        projected_asset = current_asset - reducing + adding
        if projected_asset > portfolio.equity * self.policy.max_asset_class_exposure:
            return RiskDecision(False, "asset_class_exposure_limit")
        projected_gross = portfolio.gross_exposure - reducing + adding
        if projected_gross > portfolio.equity * self.policy.max_gross_exposure:
            return RiskDecision(False, "gross_exposure_limit")
        # Exposure-opening orders must carry protection on the correct side of entry.
        if order.stop_price is not None and not _stop_on_loss_side(order):
            return RiskDecision(False, "protective_stop_wrong_side")
        if order.take_profit_price is not None and not _target_on_profit_side(order):
            return RiskDecision(False, "take_profit_wrong_side")
        return RiskDecision(True, "approved")

    @staticmethod
    def _exposure_delta(
        order: OrderIntent,
        portfolio: PortfolioSnapshot,
        notional: Decimal,
        current_symbol: Decimal,
    ) -> tuple[Decimal, Decimal]:
        """Split an order into (exposure unwound, exposure added).

        A BUY adds all of its notional. A SELL unwinds what is held and only the
        units beyond the holding add (short) exposure. When the snapshot carries
        held quantities the split is exact by units, so a full liquidation is a
        pure unwind even when the mark has drifted from the reference price; a
        snapshot without quantities falls back to the notional split.
        """
        if order.side != Side.SELL:
            return Decimal(0), notional
        held = portfolio.symbol_quantity.get(order.symbol)
        if held is None:
            reducing = min(notional, max(current_symbol, Decimal(0)))
            return reducing, notional - reducing
        if held <= 0:
            return Decimal(0), notional
        covered = min(order.quantity, held)
        reducing = max(current_symbol, Decimal(0)) * Decimal(covered) / Decimal(held)
        return reducing, order.reference_price * (order.quantity - covered)


def _stop_on_loss_side(order: OrderIntent) -> bool:
    assert order.stop_price is not None
    if order.side == Side.BUY:
        return order.stop_price < order.reference_price
    return order.stop_price > order.reference_price


def _target_on_profit_side(order: OrderIntent) -> bool:
    assert order.take_profit_price is not None
    if order.side == Side.BUY:
        return order.take_profit_price > order.reference_price
    return order.take_profit_price < order.reference_price
