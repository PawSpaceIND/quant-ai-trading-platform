from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from quant_ai.agents.contracts import AgentEvidence
from quant_ai.agents.swarm import AgentAnalysisRequest, AtlasCIOAgent, TradeProposal
from quant_ai.brokers.base import ExecutionResult
from quant_ai.domain.models import PortfolioSnapshot
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.planning.capital import CapitalPlan
from quant_ai.risk.warden import RiskWarden, WardenDecision


@dataclass(frozen=True)
class SwarmExecutionResult:
    proposal: TradeProposal
    risk_decision: WardenDecision
    fill: ExecutionResult | None


class SwarmPaperTradingService:
    """Only sanctioned MAS execution path: CIO -> Warden -> local paper broker."""

    def __init__(
        self,
        cio: AtlasCIOAgent | None = None,
        warden: RiskWarden | None = None,
        broker: PaperBrokerService | None = None,
    ) -> None:
        self.cio = cio or AtlasCIOAgent()
        self.warden = warden or RiskWarden()
        self.broker = broker or PaperBrokerService()

    def execute(
        self,
        request: AgentAnalysisRequest,
        evidence: tuple[AgentEvidence, ...],
        plan: CapitalPlan,
        portfolio: PortfolioSnapshot,
        *,
        quantity: int,
        reference_price: Decimal,
        stop_price: Decimal | None,
        take_profit_price: Decimal | None,
        country: str,
        country_exposure: dict[str, Decimal] | None = None,
        tenant_id: str = "default",
    ) -> SwarmExecutionResult:
        proposal = self.cio.propose(
            request,
            evidence,
            quantity=quantity,
            reference_price=reference_price,
            stop_price=stop_price,
            take_profit_price=take_profit_price,
            country=country,
        )
        risk = self.warden.evaluate(
            proposal,
            plan,
            portfolio,
            country_exposure=country_exposure,
            tenant_id=tenant_id,
        )
        if not risk.approved or risk.order is None:
            return SwarmExecutionResult(proposal, risk, None)
        return SwarmExecutionResult(proposal, risk, self.broker.submit(risk.order))
