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
