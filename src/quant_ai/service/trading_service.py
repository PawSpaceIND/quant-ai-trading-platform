from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from quant_ai.domain.models import AssetClass, Market, PortfolioSnapshot, Side
from quant_ai.pipeline.paper_orchestrator import (
    PaperTradeOrchestrator,
    PaperTradeRequest,
    PaperTradeResult,
)
from quant_ai.usage.metering import UsageMeter


@dataclass(frozen=True)
class ServiceTradeRequest:
    tenant_id: str
    symbol: str
    market: Market
    asset_class: AssetClass
    side: Side
    quantity: int
    entry: Decimal
    stop: Decimal
    take_profit: Decimal
    probability: Decimal
    expected_value: Decimal
    risk_amount: Decimal
    strategy_id: str
    nonce: str


class TradingService:
    def __init__(self, meter: UsageMeter | None = None) -> None:
        self.meter = meter or UsageMeter()
        self._engines: dict[str, PaperTradeOrchestrator] = {}

    def submit_paper(self, request: ServiceTradeRequest, portfolio: PortfolioSnapshot) -> PaperTradeResult:
        engine = self._engines.setdefault(request.tenant_id, PaperTradeOrchestrator())
        self.meter.increment(request.tenant_id, "paper_trade_requests")
        return engine.execute(PaperTradeRequest(
            request.symbol, request.market, request.asset_class, request.side, request.quantity,
            request.entry, request.stop, request.take_profit, request.probability,
            request.expected_value, request.risk_amount, request.strategy_id, request.nonce,
        ), portfolio)
