from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from quant_ai.audit.journal import InMemoryAuditJournal
from quant_ai.brokers.base import ExecutionResult
from quant_ai.decision.engine import DecisionEngine, DecisionInput
from quant_ai.domain.models import AssetClass, Market, OrderIntent, PortfolioSnapshot, Side
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.operations.idempotency import IdempotencyRegistry, order_idempotency_key
from quant_ai.operations.kill_switch import KillSwitch
from quant_ai.risk.policy import RiskFirewall


@dataclass(frozen=True)
class PaperTradeRequest:
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
    tenant_id: str = "default"


@dataclass(frozen=True)
class PaperTradeResult:
    approved: bool
    reason: str
    fill: ExecutionResult | None = None


class PaperTradeOrchestrator:
    def __init__(self, broker: PaperBrokerService | None = None) -> None:
        self.decision_engine = DecisionEngine()
        self.risk = RiskFirewall()
        self.broker = broker
        self.idempotency = IdempotencyRegistry()
        self.kill_switch = KillSwitch()
        self.audit = InMemoryAuditJournal()

    def execute(self, request: PaperTradeRequest, portfolio: PortfolioSnapshot) -> PaperTradeResult:
        self.kill_switch.assert_trading_allowed()
        decision = self.decision_engine.evaluate(DecisionInput(
            request.symbol,
            request.side,
            request.strategy_id,
            request.probability,
            request.expected_value,
            request.risk_amount,
            request.stop,
            request.take_profit,
        ))
        self.audit.append("DECISION", {"symbol": request.symbol, "approved": decision.approved, "reasons": decision.reasons})
        if not decision.approved:
            return PaperTradeResult(False, "decision_rejected")
        order = OrderIntent(
            request.symbol,
            request.market,
            request.side,
            request.quantity,
            request.entry,
            request.strategy_id,
            request.asset_class,
            request.tenant_id,
            request.stop,
            request.take_profit,
        )
        key = order_idempotency_key(order, request.nonce)
        if not self.idempotency.claim(key):
            self.audit.append("REJECT", {"symbol": request.symbol, "reason": "duplicate_order"})
            return PaperTradeResult(False, "duplicate_order")
        risk = self.risk.evaluate(order, portfolio)
        self.audit.append("RISK", {"symbol": request.symbol, "approved": risk.approved, "reason": risk.reason})
        if not risk.approved:
            return PaperTradeResult(False, risk.reason)
        if self.broker is None:
            self.broker = PaperBrokerService(starting_capital=portfolio.equity)
        fill = self.broker.submit(order)
        self.audit.append("FILL", {"symbol": request.symbol, "order_id": fill.order_id, "price": str(fill.average_price)})
        return PaperTradeResult(True, "filled", fill)
