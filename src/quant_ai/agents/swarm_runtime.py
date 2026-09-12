from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from quant_ai.agents.contracts import AgentEvidence
from quant_ai.agents.swarm import AgentAnalysisRequest, AtlasCIOAgent, TradeProposal
from quant_ai.analytics.attribution import AgentAttributionEngine
from quant_ai.brokers.base import ExecutionResult
from quant_ai.domain.models import PortfolioSnapshot
from quant_ai.execution.audit import XAITrace, XAITraceLogger
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.intelligence.adversarial import AdversarialStressAgent, StressVerdict
from quant_ai.planning.capital import CapitalPlan
from quant_ai.risk.warden import RiskWarden, WardenDecision


@dataclass(frozen=True)
class SwarmExecutionResult:
    proposal: TradeProposal
    risk_decision: WardenDecision
    fill: ExecutionResult | None
    stress_verdict: StressVerdict
    xai_trace: XAITrace


class SwarmPaperTradingService:
    """Only sanctioned MAS execution path: CIO -> Warden -> local paper broker."""

    def __init__(
        self,
        cio: AtlasCIOAgent | None = None,
        warden: RiskWarden | None = None,
        broker: PaperBrokerService | None = None,
        attribution: AgentAttributionEngine | None = None,
        stress_agent: AdversarialStressAgent | None = None,
        xai_logger: XAITraceLogger | None = None,
    ) -> None:
        self.cio = cio or AtlasCIOAgent()
        self.warden = warden or RiskWarden()
        self.broker = broker or PaperBrokerService()
        self.attribution = attribution or AgentAttributionEngine()
        self.stress_agent = stress_agent or AdversarialStressAgent()
        self.xai_logger = xai_logger or XAITraceLogger()

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
        weighted = self.attribution.weight_evidence(evidence)
        proposal = self.cio.propose(
            request, weighted, quantity=quantity, reference_price=reference_price,
            stop_price=stop_price, take_profit_price=take_profit_price, country=country,
        )
        return self._execute_proposal(
            request, weighted, proposal, plan, portfolio, country_exposure, tenant_id
        )

    async def execute_async(
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
        market_tick: object | None = None,
        country_exposure: dict[str, Decimal] | None = None,
        tenant_id: str = "default",
    ) -> SwarmExecutionResult:
        weighted = self.attribution.weight_evidence(evidence)
        proposal = await self.cio.propose_async(
            request, weighted, quantity=quantity, reference_price=reference_price,
            stop_price=stop_price, take_profit_price=take_profit_price, country=country,
            market_tick=market_tick,
        )
        return self._execute_proposal(
            request, weighted, proposal, plan, portfolio, country_exposure, tenant_id
        )

    def _execute_proposal(
        self,
        request: AgentAnalysisRequest,
        weighted_evidence: tuple[AgentEvidence, ...],
        proposal: TradeProposal,
        plan: CapitalPlan,
        portfolio: PortfolioSnapshot,
        country_exposure: dict[str, Decimal] | None,
        tenant_id: str,
    ) -> SwarmExecutionResult:
        stress = self.stress_agent.evaluate(proposal, portfolio)
        if not stress.passed:
            risk = self.warden.reject("STRESS_VETO", proposal, tenant_id)
            trace = self.xai_logger.log(request, weighted_evidence, proposal, stress, risk)
            return SwarmExecutionResult(proposal, risk, None, stress, trace)
        risk = self.warden.evaluate(
            proposal, plan, portfolio, country_exposure=country_exposure, tenant_id=tenant_id
        )
        if risk.approved and risk.order is not None and risk.order.side.value == "SELL":
            held = sum(
                position.quantity
                for position in self.broker.get_positions(tenant_id)
                if position.symbol == risk.order.symbol
                and position.market == risk.order.market
                and position.asset_class == risk.order.asset_class
            )
            if held < risk.order.quantity:
                risk = self.warden.reject("paper_naked_sell_disabled", proposal, tenant_id)
        trace = self.xai_logger.log(request, weighted_evidence, proposal, stress, risk)
        if not risk.approved or risk.order is None:
            return SwarmExecutionResult(proposal, risk, None, stress, trace)
        return SwarmExecutionResult(
            proposal, risk, self.broker.submit(risk.order), stress, trace
        )
