from decimal import Decimal

from quant_ai.agents.swarm import TradeProposal
from quant_ai.domain.models import AssetClass, Market, PortfolioSnapshot, RiskMode, Side
from quant_ai.notifications.trading import TradingAlertCode, TradingNotificationDispatcher
from quant_ai.planning.capital import CapitalGoalEngine, CapitalPlanRequest
from quant_ai.risk.warden import RiskWarden


def plan():
    return CapitalGoalEngine().recommend(CapitalPlanRequest(
        Decimal(100000), Decimal("0.75"), Decimal("0.20"),
        expected_edge=Decimal("0.02"), requested_mode=RiskMode.BALANCED,
    ))


def proposal(quantity: int = 10) -> TradeProposal:
    return TradeProposal(
        "decision-1", "AAPL", Market.USA, "USA", AssetClass.EQUITY, Side.BUY,
        quantity, Decimal(100), Decimal(95), Decimal(110), Decimal("0.75"),
        Decimal("0.03"), Decimal("0.02"), ("consensus",),
    )


def portfolio(**kwargs) -> PortfolioSnapshot:
    base = {
        "equity": Decimal(100000),
        "daily_realized_pnl": Decimal(0),
        "gross_exposure": Decimal(0),
        "peak_equity": Decimal(100000),
        "symbol_exposure": {},
        "asset_exposure": {},
    }
    base.update(kwargs)
    return PortfolioSnapshot(**base)


def test_risk_warden_approves_only_after_hard_firewall() -> None:
    decision = RiskWarden().evaluate(proposal(), plan(), portfolio(), country_exposure={"USA": Decimal(0)})
    assert decision.approved
    assert decision.order is not None
    assert decision.order.stop_price == Decimal(95)


def test_risk_warden_rejects_country_limit_and_dispatches_alert() -> None:
    dispatcher = TradingNotificationDispatcher(sinks=())
    decision = RiskWarden(dispatcher).evaluate(
        proposal(100), plan(), portfolio(), country_exposure={"USA": Decimal(49500)}, tenant_id="t1"
    )
    assert not decision.approved
    assert decision.reason == "country_allocation_limit"
    alert = dispatcher.pending("t1")[-1]
    assert alert.code == TradingAlertCode.RISK_PROPOSAL_REJECTED
    assert alert.metadata["reason"] == "country_allocation_limit"


def test_risk_warden_rejects_dynamic_position_limit() -> None:
    dispatcher = TradingNotificationDispatcher(sinks=())
    decision = RiskWarden(dispatcher).evaluate(
        proposal(101), plan(), portfolio(), country_exposure={"USA": Decimal(0)}
    )
    assert not decision.approved
    assert decision.reason == "single_trade_notional_limit"


def test_risk_warden_rejects_drawdown_limit() -> None:
    dispatcher = TradingNotificationDispatcher(sinks=())
    p = plan()
    current_equity = Decimal(89000)
    decision = RiskWarden(dispatcher).evaluate(
        proposal(), p,
        portfolio(equity=current_equity, peak_equity=Decimal(100000)),
        country_exposure={"USA": Decimal(0)},
    )
    assert not decision.approved
    assert decision.reason == "max_drawdown_reached"


def test_risk_warden_rejects_missing_protective_stop() -> None:
    dispatcher = TradingNotificationDispatcher(sinks=())
    p = proposal()
    no_stop = TradeProposal(
        p.decision_id, p.symbol, p.market, p.country, p.asset_class, p.side, p.quantity,
        p.reference_price, None, p.take_profit_price, p.confidence, p.expected_return,
        p.expected_risk, p.rationale,
    )
    decision = RiskWarden(dispatcher).evaluate(no_stop, plan(), portfolio())
    assert not decision.approved
    assert decision.reason == "protective_stop_required"


def test_dynamic_plan_cannot_relax_baseline_firewall() -> None:
    dispatcher = TradingNotificationDispatcher(sinks=())
    decision = RiskWarden(dispatcher).evaluate(
        proposal(60), plan(), portfolio(), country_exposure={"USA": Decimal(0)}
    )
    assert not decision.approved
    assert decision.reason == "single_trade_notional_limit"
