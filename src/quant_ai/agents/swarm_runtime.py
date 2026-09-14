from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from quant_ai.agents.contracts import AgentEvidence
from quant_ai.agents.swarm import AgentAnalysisRequest, AtlasCIOAgent, TradeProposal
from quant_ai.analytics.attribution import AgentAttributionEngine
from quant_ai.brokers.base import ExecutionResult
from quant_ai.domain.models import OrderIntent, PortfolioSnapshot, Side
from quant_ai.execution.audit import XAITrace, XAITraceLogger
from quant_ai.execution.paper_ledger import PaperBrokerDatabaseLockedError, PaperBrokerService
from quant_ai.intelligence.adversarial import AdversarialStressAgent, StressVerdict
from quant_ai.operations.idempotency import order_idempotency_key
from quant_ai.operations.kill_switch import KillSwitch
from quant_ai.orders.state import OrderLifecycle, OrderState
from quant_ai.planning.capital import CapitalPlan
from quant_ai.risk.warden import RiskWarden, WardenDecision


@dataclass(frozen=True)
class SwarmExecutionResult:
    proposal: TradeProposal
    risk_decision: WardenDecision
    fill: ExecutionResult | None
    stress_verdict: StressVerdict
    xai_trace: XAITrace
    order_state: OrderState = OrderState.CREATED


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
        kill_switch: KillSwitch | None = None,
        *,
        allow_position_scaling: bool = False,
    ) -> None:
        self.cio = cio or AtlasCIOAgent()
        self.warden = warden or RiskWarden()
        self.broker = broker or PaperBrokerService()
        self.attribution = attribution or AgentAttributionEngine()
        self.stress_agent = stress_agent or AdversarialStressAgent()
        self.xai_logger = xai_logger or XAITraceLogger()
        # C5: the daemon now shares the controls that already protected the HTTP path.
        self.kill_switch = kill_switch or KillSwitch()
        # C2: entering a symbol that is already held requires an explicit opt-in.
        self.allow_position_scaling = allow_position_scaling

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
        preflight_veto_reason: str | None = None,
        country_exposure: dict[str, Decimal] | None = None,
        tenant_id: str = "default",
    ) -> SwarmExecutionResult:
        weighted = self.attribution.weight_evidence(evidence)
        if preflight_veto_reason is not None:
            proposal = self.cio.propose(
                request, weighted, quantity=quantity, reference_price=reference_price,
                stop_price=stop_price, take_profit_price=take_profit_price, country=country,
            )
            stress = self.stress_agent.evaluate(proposal, portfolio)
            risk = self.warden.reject(preflight_veto_reason, proposal, tenant_id)
            trace = self.xai_logger.log(request, weighted, proposal, stress, risk)
            return SwarmExecutionResult(
                proposal, risk, None, stress, trace, OrderState.REJECTED
            )
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
        lifecycle = OrderLifecycle()
        stress = self.stress_agent.evaluate(proposal, portfolio)

        def refuse(reason: str) -> SwarmExecutionResult:
            rejected = self.warden.reject(reason, proposal, tenant_id)
            if lifecycle.state != OrderState.REJECTED:
                lifecycle.transition(OrderState.REJECTED)
            trace = self.xai_logger.log(request, weighted_evidence, proposal, stress, rejected)
            return SwarmExecutionResult(
                proposal, rejected, None, stress, trace, lifecycle.state
            )

        # C5: an engaged kill switch halts the daemon before anything else is evaluated.
        if self.kill_switch.engaged:
            return refuse(f"kill_switch_engaged:{self.kill_switch.reason}")
        if not stress.passed:
            return refuse("STRESS_VETO")

        risk = self.warden.evaluate(
            proposal, plan, portfolio, country_exposure=country_exposure, tenant_id=tenant_id
        )
        if not risk.approved or risk.order is None:
            trace = self.xai_logger.log(request, weighted_evidence, proposal, stress, risk)
            lifecycle.transition(OrderState.REJECTED)
            return SwarmExecutionResult(proposal, risk, None, stress, trace, lifecycle.state)

        held = self._held_quantity(risk.order, tenant_id)
        # C2: block a second entry into a symbol that is already open.
        if risk.order.side == Side.BUY and held > 0 and not self.allow_position_scaling:
            return refuse("position_already_open")
        # C2 (extension): and do not immediately re-enter a symbol just stopped out.
        if risk.order.side == Side.BUY and self._in_exit_cooldown(
            risk.order, tenant_id, request.observed_at
        ):
            return refuse("re_entry_cooldown_active")
        if risk.order.side == Side.SELL and held < risk.order.quantity:
            return refuse("paper_naked_sell_disabled")

        lifecycle.transition(OrderState.RISK_APPROVED)

        # C5: durable replay guard. The claim is stored in SQLite, so a restart mid-cadence
        # cannot resubmit an order that already reached the broker.
        key = order_idempotency_key(risk.order, proposal.decision_id)
        if not self.broker.claim_idempotency_key(key.value, tenant_id):
            return refuse("duplicate_order")

        lifecycle.transition(OrderState.SUBMITTED)
        try:
            fill = self.broker.submit(risk.order)
        except PaperBrokerDatabaseLockedError:
            return refuse("paper_broker_database_locked")
        except ValueError as error:
            # C3: a broker-side rejection (insufficient cash/position) is a governed outcome,
            # not a daemon-killing exception.
            return refuse(f"broker_rejected:{error}")
        lifecycle.transition(OrderState.FILLED)
        # The proof is written after the fill so it carries the broker order id: the UI
        # joins proofs to ledger rows on that id and refuses any looser association.
        trace = self.xai_logger.log(
            request, weighted_evidence, proposal, stress, risk, fill=fill
        )
        return SwarmExecutionResult(proposal, risk, fill, stress, trace, lifecycle.state)

    def _in_exit_cooldown(
        self, order: OrderIntent, tenant_id: str, now: datetime
    ) -> bool:
        until = self.broker.exit_cooldown_until(
            order.symbol, order.market, order.asset_class, tenant_id
        )
        if until is None:
            return False
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        reference = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        return reference < until

    def _held_quantity(self, order: OrderIntent, tenant_id: str) -> int:
        return sum(
            position.quantity
            for position in self.broker.get_positions(tenant_id)
            if position.symbol == order.symbol
            and position.market == order.market
            and position.asset_class == order.asset_class
        )
