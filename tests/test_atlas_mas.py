from datetime import datetime, timezone
from decimal import Decimal

from quant_ai.agents.contracts import Stance
from quant_ai.agents.swarm import (
    AgentAnalysisRequest,
    AtlasCIOAgent,
    CommodityYieldAgent,
    GeopoliticalAnalystAgent,
    IndianEquitiesAgent,
    TechnicalQuantAgent,
    USEquitiesAgent,
)
from quant_ai.domain.models import AssetClass, Market


def request(market: Market = Market.USA) -> AgentAnalysisRequest:
    return AgentAnalysisRequest(
        "AAPL" if market == Market.USA else "RELIANCE",
        market,
        AssetClass.EQUITY,
        datetime.now(timezone.utc),
        {
            "news_sentiment": Decimal("0.60"),
            "geopolitical_risk": Decimal("0.10"),
            "commodity_momentum": Decimal("0.35"),
            "yield_pressure": Decimal("0.05"),
            "india_equity_score": Decimal("0.50"),
            "us_equity_score": Decimal("0.70"),
            "momentum": Decimal("0.80"),
            "trend": Decimal("0.70"),
        },
    )


def test_specialist_agents_emit_canonical_evidence() -> None:
    req = request()
    agents = (
        GeopoliticalAnalystAgent(),
        CommodityYieldAgent(),
        IndianEquitiesAgent(),
        USEquitiesAgent(),
        TechnicalQuantAgent(),
    )
    evidence = tuple(agent.analyze(req) for agent in agents)
    assert len(evidence) == 5
    assert all(item.subject == "AAPL" for item in evidence)
    assert all(Decimal(0) <= item.confidence <= Decimal(1) for item in evidence)
    assert evidence[-1].stance in {Stance.BUY, Stance.STRONG_BUY}



def test_us_equities_refuses_india_market() -> None:
    evidence = USEquitiesAgent().analyze(request(Market.INDIA))
    assert evidence.stance == Stance.NEUTRAL
    assert "non_us_market" in evidence.rationale

def test_atlas_cio_creates_proposal_but_does_not_execute() -> None:
    req = request()
    agents = (
        GeopoliticalAnalystAgent(), CommodityYieldAgent(), IndianEquitiesAgent(),
        USEquitiesAgent(), TechnicalQuantAgent(),
    )
    evidence = tuple(agent.analyze(req) for agent in agents)
    proposal = AtlasCIOAgent().propose(
        req,
        evidence,
        quantity=10,
        reference_price=Decimal(100),
        stop_price=Decimal(95),
        take_profit_price=Decimal(110),
        country="USA",
    )
    assert proposal.symbol == "AAPL"
    assert proposal.side is not None
    assert proposal.quantity == 10
    assert not hasattr(AtlasCIOAgent(), "submit")
