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


def make_sell(quantity: int = 10, price: str = "100", stop: str | None = "102") -> OrderIntent:
    return OrderIntent(
        "TEST", Market.INDIA, Side.SELL, quantity, Decimal(price), "test",
        AssetClass.EQUITY, stop_price=Decimal(stop) if stop else None,
    )


def held_portfolio(units: int = 100, mark: str = "100", **overrides) -> PortfolioSnapshot:
    """``units`` of TEST held, marked at ``mark``, with held quantities in the snapshot."""
    value = Decimal(mark) * units
    base = {
        "equity": Decimal(100000), "daily_realized_pnl": Decimal(0), "gross_exposure": value,
        "peak_equity": Decimal(100000), "symbol_exposure": {"TEST": value},
        "asset_exposure": {AssetClass.EQUITY: value}, "symbol_quantity": {"TEST": units},
    }
    base.update(overrides)
    return PortfolioSnapshot(**base)


def test_full_liquidation_is_a_pure_unwind_despite_mark_drift() -> None:
    # Marked at 99 but sold at a 100 reference: by notional the sell "exceeds" the
    # holding by 100 and would fall through to the halts; by units it is exactly flat.
    breached = held_portfolio(units=100, mark="99", equity=Decimal(89000))
    decision = RiskFirewall().evaluate(make_sell(quantity=100, price="100", stop=None), breached)
    assert decision.approved
    assert decision.reason == "approved_risk_reducing"


def test_units_beyond_the_holding_still_add_exposure() -> None:
    decision = RiskFirewall().evaluate(make_sell(quantity=300, stop="102"), held_portfolio())
    assert not decision.approved
    assert decision.reason == "single_trade_notional_limit"


def test_rejects_stop_on_wrong_side_for_exposure_opening_orders() -> None:
    firewall = RiskFirewall()
    buy_stop_above = firewall.evaluate(make_order(stop="102"), make_portfolio())
    assert buy_stop_above.reason == "protective_stop_wrong_side"
    # 110 sold against 100 held: 10 units open a short, and its stop sits below the entry.
    short_stop_below = firewall.evaluate(make_sell(quantity=110, stop="98"), held_portfolio())
    assert short_stop_below.reason == "protective_stop_wrong_side"
    short_stop_above = firewall.evaluate(make_sell(quantity=110, stop="102"), held_portfolio())
    assert short_stop_above.approved, short_stop_above.reason


def test_pure_unwind_is_never_held_to_stop_geometry() -> None:
    decision = RiskFirewall().evaluate(make_sell(quantity=50, stop="98"), held_portfolio())
    assert decision.reason == "approved_risk_reducing"
