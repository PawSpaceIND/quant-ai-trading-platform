from datetime import datetime, timezone
from decimal import Decimal

from quant_ai.agents.swarm import (
    AgentAnalysisRequest,
    CommodityYieldAgent,
    GeopoliticalAnalystAgent,
    IndianEquitiesAgent,
    TechnicalQuantAgent,
    USEquitiesAgent,
)
from quant_ai.agents.swarm_runtime import SwarmPaperTradingService
from quant_ai.domain.models import AssetClass, Market, PortfolioSnapshot, RiskMode
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.planning.capital import CapitalGoalEngine, CapitalPlanRequest


def test_swarm_execution_can_only_reach_paper_ledger(tmp_path) -> None:
    request = AgentAnalysisRequest(
        "AAPL", Market.USA, AssetClass.EQUITY, datetime.now(timezone.utc),
        {
            "news_sentiment": Decimal("0.8"), "geopolitical_risk": Decimal("0.1"),
            "commodity_momentum": Decimal("0.5"), "yield_pressure": Decimal("0.1"),
            "india_equity_score": Decimal("0.4"), "us_equity_score": Decimal("0.8"),
            "momentum": Decimal("0.9"), "trend": Decimal("0.8"),
        },
    )
    agents = (
        GeopoliticalAnalystAgent(), CommodityYieldAgent(), IndianEquitiesAgent(),
        USEquitiesAgent(), TechnicalQuantAgent(),
    )
    evidence = tuple(agent.analyze(request) for agent in agents)
    plan = CapitalGoalEngine().recommend(CapitalPlanRequest(
        Decimal(100000), Decimal("0.8"), Decimal("0.2"),
        expected_edge=Decimal("0.02"), requested_mode=RiskMode.BALANCED,
    ))
    portfolio = PortfolioSnapshot(Decimal(100000), Decimal(0), Decimal(0))
    broker = PaperBrokerService(tmp_path / "mas-paper.db", starting_capital=Decimal(100000), slippage_bps=Decimal(0))
    result = SwarmPaperTradingService(broker=broker).execute(
        request, evidence, plan, portfolio,
        quantity=10, reference_price=Decimal(100), stop_price=Decimal(95),
        take_profit_price=Decimal(110), country="USA", tenant_id="tenant-mas",
    )
    assert result.risk_decision.approved
    assert result.fill is not None
    assert result.fill.order_id.startswith("PAPER-")
    assert len(broker.ledger_entries("tenant-mas")) == 1


from quant_ai.agents.contracts import AgentDomain, AgentEvidence, Stance
from quant_ai.domain.models import OrderIntent, Side


def _evidence(stance: Stance, now):
    return tuple(
        AgentEvidence(
            f"agent-{index}", AgentDomain.TECHNICAL, "AAPL", stance,
            Decimal("0.8"), Decimal("0.02"), Decimal("0.01"), ("declared",), now, 0,
        )
        for index in range(4)
    )


def _seed_position(broker, quantity: int) -> PortfolioSnapshot:
    broker.buy(OrderIntent(
        "AAPL", Market.USA, Side.BUY, quantity, Decimal(100), "seed", AssetClass.EQUITY,
        "cap", Decimal(95), Decimal(110),
    ))
    value = Decimal(100 * quantity)
    return PortfolioSnapshot(
        Decimal(100000), Decimal(0), value, peak_equity=Decimal(100000),
        symbol_exposure={"AAPL": value}, asset_exposure={AssetClass.EQUITY: value},
        symbol_quantity={"AAPL": quantity},
    )


def _plan():
    return CapitalGoalEngine().recommend(CapitalPlanRequest(
        Decimal(100000), Decimal("0.8"), Decimal("0.2"),
        expected_edge=Decimal("0.02"), requested_mode=RiskMode.BALANCED,
    ))


def _execute(service, stance, snapshot, quantity, now, tenant="cap"):
    request = AgentAnalysisRequest("AAPL", Market.USA, AssetClass.EQUITY, now, {}, 0)
    return service.execute(
        request, _evidence(stance, now), _plan(), snapshot, quantity=quantity,
        reference_price=Decimal(100), stop_price=Decimal(95), take_profit_price=Decimal(110),
        country="USA", tenant_id=tenant,
    )


def test_sell_is_capped_at_holding_and_unsized_sell_exits_fully(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    broker = PaperBrokerService(tmp_path / "cap.db", starting_capital=Decimal(100000), slippage_bps=Decimal(0))
    service = SwarmPaperTradingService(broker=broker)

    oversized = _execute(service, Stance.STRONG_SELL, _seed_position(broker, 30), 100, now)
    assert oversized.risk_decision.approved, oversized.risk_decision.reason
    assert oversized.proposal.quantity == 30
    assert oversized.fill is not None and oversized.fill.filled_quantity == 30
    assert broker.get_positions("cap") == ()

    unsized = _execute(service, Stance.STRONG_SELL, _seed_position(broker, 12), 0, now)
    assert unsized.fill is not None and unsized.fill.filled_quantity == 12
    assert broker.get_positions("cap") == ()

    flat = PortfolioSnapshot(Decimal(100000), Decimal(0), Decimal(0), peak_equity=Decimal(100000))
    naked = _execute(service, Stance.STRONG_SELL, flat, 10, now)
    assert naked.fill is None
    assert naked.risk_decision.reason == "paper_naked_sell_disabled"


def test_unsized_buy_is_refused_before_it_reaches_the_broker(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    broker = PaperBrokerService(tmp_path / "zero.db", starting_capital=Decimal(100000), slippage_bps=Decimal(0))
    flat = PortfolioSnapshot(Decimal(100000), Decimal(0), Decimal(0), peak_equity=Decimal(100000))
    result = _execute(SwarmPaperTradingService(broker=broker), Stance.STRONG_BUY, flat, 0, now, tenant="zero")
    assert result.fill is None
    assert result.risk_decision.reason in {"position_sizer_no_capacity", "invalid_trade_proposal"}
    assert broker.ledger_entries("zero") == ()
