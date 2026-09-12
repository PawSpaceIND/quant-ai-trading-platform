from datetime import datetime, timezone
from decimal import Decimal

from quant_ai.agents.swarm import TradeProposal
from quant_ai.domain.models import AssetClass, Market, OrderIntent, RiskMode, Side
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.execution.portfolio import PortfolioTracker
from quant_ai.marketdata.feed import UsaSandboxMarketDataFeed
from quant_ai.planning.capital import CapitalGoalEngine, CapitalPlanRequest
from quant_ai.risk.warden import RiskWarden


def order(side: Side, qty: int, price: str) -> OrderIntent:
    return OrderIntent(
        "AAPL", Market.USA, side, qty, Decimal(price), "test", AssetClass.EQUITY,
        "tenant", Decimal(90), Decimal(130)
    )


def test_mtm_average_entry_unrealized_and_realized_partial_full_exits(tmp_path) -> None:
    broker = PaperBrokerService(tmp_path / "mtm.db", starting_capital=Decimal(100000), slippage_bps=Decimal(0))
    feed = UsaSandboxMarketDataFeed(base_price=Decimal(130))
    tracker = PortfolioTracker(broker, feed, tenant_id="tenant")
    broker.buy(order(Side.BUY, 10, "100"))
    broker.buy(order(Side.BUY, 10, "120"))
    metrics = tracker.metrics(datetime.now(timezone.utc))
    assert metrics.positions[0].average_entry_price == Decimal(110)
    assert metrics.unrealized_pnl == Decimal(400)
    assert metrics.total_equity == Decimal(100400)

    broker.sell(order(Side.SELL, 5, "140"))
    metrics = tracker.metrics(datetime.now(timezone.utc))
    assert metrics.realized_pnl == Decimal(150)
    assert metrics.positions[0].quantity == 15

    broker.sell(order(Side.SELL, 15, "130"))
    metrics = tracker.metrics(datetime.now(timezone.utc))
    assert metrics.realized_pnl == Decimal(450)
    assert metrics.positions == ()
    assert metrics.total_equity == Decimal(100450)


def test_drawdown_snapshot_triggers_risk_warden(tmp_path) -> None:
    broker = PaperBrokerService(tmp_path / "drawdown.db", starting_capital=Decimal(100000), slippage_bps=Decimal(0))
    feed = UsaSandboxMarketDataFeed(base_price=Decimal(100))
    tracker = PortfolioTracker(broker, feed, tenant_id="tenant")
    broker.buy(order(Side.BUY, 500, "100"))
    tracker.metrics(datetime.now(timezone.utc))
    feed.base_price = Decimal(50)
    snapshot = tracker.get_snapshot(datetime.now(timezone.utc))
    assert snapshot.peak_equity == Decimal(100000)
    assert snapshot.equity == Decimal(75000)

    plan = CapitalGoalEngine().recommend(CapitalPlanRequest(
        Decimal(100000), Decimal("0.8"), Decimal("0.2"), expected_edge=Decimal("0.02"),
        requested_mode=RiskMode.BALANCED,
    ))
    proposal = TradeProposal(
        "dd", "AAPL", Market.USA, "USA", AssetClass.EQUITY, Side.BUY, 1,
        Decimal(50), Decimal(45), Decimal(60), Decimal("0.8"), Decimal("0.02"),
        Decimal("0.01"), ("test",),
    )
    decision = RiskWarden().evaluate(proposal, plan, snapshot, tenant_id="tenant")
    assert not decision.approved
    assert decision.reason == "max_drawdown_reached"
