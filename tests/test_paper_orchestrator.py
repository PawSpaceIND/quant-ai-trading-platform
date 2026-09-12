from decimal import Decimal

from quant_ai.domain.models import AssetClass, Market, PortfolioSnapshot, Side
from quant_ai.pipeline.paper_orchestrator import PaperTradeOrchestrator, PaperTradeRequest


def portfolio() -> PortfolioSnapshot:
    return PortfolioSnapshot(Decimal(100000), Decimal(0), Decimal(0))


def request(nonce: str = "bar-1") -> PaperTradeRequest:
    return PaperTradeRequest(
        "AAPL", Market.USA, AssetClass.EQUITY, Side.BUY, 10,
        Decimal(100), Decimal(95), Decimal(110), Decimal("0.70"),
        Decimal(25), Decimal(500), "momentum", nonce,
    )


def test_orchestrator_fills_and_audits() -> None:
    orchestrator = PaperTradeOrchestrator()
    result = orchestrator.execute(request(), portfolio())
    assert result.approved
    assert result.fill and result.fill.status == "FILLED"
    assert orchestrator.broker is not None
    assert len(orchestrator.broker.ledger_entries()) == 1
    assert [event.event_type for event in orchestrator.audit.events()] == ["DECISION", "RISK", "FILL"]
    assert orchestrator.audit.verify_chain()


def test_orchestrator_blocks_duplicate() -> None:
    orchestrator = PaperTradeOrchestrator()
    assert orchestrator.execute(request(), portfolio()).approved
    second = orchestrator.execute(request(), portfolio())
    assert not second.approved
    assert second.reason == "duplicate_order"
