from decimal import Decimal

from quant_ai.domain.models import Market, OrderIntent, PortfolioSnapshot, Side
from quant_ai.risk.policy import RiskFirewall


def make_order(quantity: int = 10, price: str = "100") -> OrderIntent:
    return OrderIntent("TEST", Market.INDIA, Side.BUY, quantity, Decimal(price), "test")


def make_portfolio(pnl: str = "0", exposure: str = "0") -> PortfolioSnapshot:
    return PortfolioSnapshot(Decimal(100000), Decimal(pnl), Decimal(exposure))


def test_approves_bounded_trade() -> None:
    assert RiskFirewall().evaluate(make_order(), make_portfolio()).approved


def test_rejects_daily_loss_limit() -> None:
    decision = RiskFirewall().evaluate(make_order(), make_portfolio(pnl="-2000"))
    assert not decision.approved
    assert decision.reason == "daily_loss_limit_reached"


def test_rejects_large_single_trade() -> None:
    decision = RiskFirewall().evaluate(make_order(quantity=100, price="100"), make_portfolio())
    assert not decision.approved
    assert decision.reason == "single_trade_notional_limit"
