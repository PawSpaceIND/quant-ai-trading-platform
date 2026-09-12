from __future__ import annotations

from decimal import Decimal

from quant_ai.domain.models import AssetClass, Market, OrderIntent, PortfolioSnapshot, Side
from quant_ai.risk.policy import RiskFirewall


def make_order(quantity: int = 10, price: str = "100", stop: str | None = "98") -> OrderIntent:
    return OrderIntent(
        "TEST", Market.INDIA, Side.BUY, quantity, Decimal(price), "test",
        AssetClass.EQUITY, stop_price=Decimal(stop) if stop else None,
    )


def make_portfolio(pnl: str = "0", exposure: str = "0", equity: str = "100000", peak: str = "100000") -> PortfolioSnapshot:
    return PortfolioSnapshot(Decimal(equity), Decimal(pnl), Decimal(exposure), Decimal(peak))


def test_approves_bounded_trade() -> None:
    assert RiskFirewall().evaluate(make_order(), make_portfolio()).approved


def test_rejects_missing_stop() -> None:
    decision = RiskFirewall().evaluate(make_order(stop=None), make_portfolio())
    assert not decision.approved
    assert decision.reason == "protective_stop_required"


def test_rejects_daily_loss_limit() -> None:
    decision = RiskFirewall().evaluate(make_order(), make_portfolio(pnl="-2000"))
    assert not decision.approved
    assert decision.reason == "daily_loss_limit_reached"


def test_rejects_drawdown_limit() -> None:
    decision = RiskFirewall().evaluate(make_order(), make_portfolio(equity="89000", peak="100000"))
    assert not decision.approved
    assert decision.reason == "max_drawdown_reached"


def test_rejects_large_single_trade() -> None:
    decision = RiskFirewall().evaluate(make_order(quantity=100, price="100"), make_portfolio())
    assert not decision.approved
    assert decision.reason == "single_trade_notional_limit"
