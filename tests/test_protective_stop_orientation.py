from datetime import datetime, timezone
from decimal import Decimal

from quant_ai.agents.contracts import AgentDomain, AgentEvidence, Stance
from quant_ai.agents.swarm import AgentAnalysisRequest, AtlasCIOAgent
from quant_ai.agents.swarm_runtime import SwarmPaperTradingService
from quant_ai.domain.models import (
    AssetClass,
    Market,
    OrderIntent,
    PortfolioSnapshot,
    RiskMode,
    Side,
)
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.planning.capital import CapitalGoalEngine, CapitalPlanRequest
from quant_ai.risk.stops import orient_protective_levels

NOW = datetime(2026, 9, 14, 14, tzinfo=timezone.utc)


def evidence(stance: Stance, count: int = 4) -> tuple[AgentEvidence, ...]:
    return tuple(
        AgentEvidence(
            f"agent-{index}", AgentDomain.TECHNICAL, "AAPL", stance,
            Decimal("0.8"), Decimal("0.02"), Decimal("0.01"), ("declared",), NOW, 0,
        )
        for index in range(count)
    )


def request() -> AgentAnalysisRequest:
    return AgentAnalysisRequest("AAPL", Market.USA, AssetClass.EQUITY, NOW, {}, 0)


def plan():
    return CapitalGoalEngine().recommend(CapitalPlanRequest(
        Decimal(100000), Decimal("0.8"), Decimal("0.2"),
        expected_edge=Decimal("0.02"), requested_mode=RiskMode.BALANCED,
    ))


def test_orient_levels_by_side_is_exact_and_idempotent() -> None:
    assert orient_protective_levels(Side.BUY, Decimal(100), Decimal(95), Decimal(110)) == (Decimal(95), Decimal(110))
    assert orient_protective_levels(Side.SELL, Decimal(100), Decimal(95), Decimal(110)) == (Decimal(105), Decimal(90))
    assert orient_protective_levels(Side.SELL, Decimal(100), Decimal(105), Decimal(90)) == (Decimal(105), Decimal(90))
    assert orient_protective_levels(None, Decimal(100), Decimal(95), Decimal(110)) == (Decimal(95), Decimal(110))
    assert orient_protective_levels(Side.SELL, Decimal(100), None, None) == (None, None)


def test_atlas_sell_proposal_carries_stop_above_and_take_profit_below() -> None:
    proposal = AtlasCIOAgent().propose(
        request(), evidence(Stance.STRONG_SELL), quantity=10, reference_price=Decimal(100),
        stop_price=Decimal(95), take_profit_price=Decimal(110), country="USA",
    )
    assert proposal.side == Side.SELL
    assert proposal.stop_price == Decimal(105)
    assert proposal.take_profit_price == Decimal(90)


def test_atlas_buy_proposal_keeps_long_orientation() -> None:
    proposal = AtlasCIOAgent().propose(
        request(), evidence(Stance.STRONG_BUY), quantity=10, reference_price=Decimal(100),
        stop_price=Decimal(95), take_profit_price=Decimal(110), country="USA",
    )
    assert proposal.side == Side.BUY
    assert proposal.stop_price == Decimal(95)
    assert proposal.take_profit_price == Decimal(110)


def test_sell_exit_carries_oriented_stop_and_reduces_position(tmp_path) -> None:
    broker = PaperBrokerService(tmp_path / "exit.db", starting_capital=Decimal(100000), slippage_bps=Decimal(0))
    broker.buy(OrderIntent(
        "AAPL", Market.USA, Side.BUY, 30, Decimal(100), "seed", AssetClass.EQUITY, "t",
        Decimal(95), Decimal(110),
    ))
    snapshot = PortfolioSnapshot(
        Decimal(100000), Decimal(0), Decimal(3000), peak_equity=Decimal(100000),
        symbol_exposure={"AAPL": Decimal(3000)}, asset_exposure={AssetClass.EQUITY: Decimal(3000)},
        symbol_quantity={"AAPL": 30},
    )
    result = SwarmPaperTradingService(broker=broker).execute(
        request(), evidence(Stance.STRONG_SELL), plan(), snapshot,
        quantity=10, reference_price=Decimal(100), stop_price=Decimal(95),
        take_profit_price=Decimal(110), country="USA", tenant_id="t",
    )
    assert result.risk_decision.approved, result.risk_decision.reason
    assert result.risk_decision.order is not None
    assert result.risk_decision.order.side == Side.SELL
    assert result.risk_decision.order.stop_price == Decimal(105)
    assert result.fill is not None and result.fill.filled_quantity == 10
    assert broker.get_positions("t")[0].quantity == 20
