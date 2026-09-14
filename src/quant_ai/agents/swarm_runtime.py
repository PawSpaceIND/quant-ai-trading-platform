from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, replace
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

LOGGER = logging.getLogger(__name__)


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
        max_open_positions: int | None = None,
    ) -> None:
        if max_open_positions is not None and max_open_positions < 1:
            raise ValueError("max_open_positions must be at least one")
        self.snapshot_provider = None
        self.pre_submit_check = None
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
        # Founder scope: how many symbols may be open at once across the book.
        self.max_open_positions = max_open_positions

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

    def _execute_proposal(self, request, weighted_evidence, proposal, plan, portfolio,
                          country_exposure, tenant_id):
        with self.broker._lock:
            if self.snapshot_provider is not None:
                portfolio = self.snapshot_provider()
                country_exposure = portfolio.country_exposure
            return self._execute_proposal_locked(request, weighted_evidence, proposal, plan,
                                                 portfolio, country_exposure, tenant_id)

    def _execute_proposal_locked(
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
        held = self._held_quantity(proposal, tenant_id)
        if proposal.side == Side.SELL and held > 0:
            # A SELL never exceeds the holding, and an unsized SELL (the sizer found no
            # entry capacity, so quantity is 0) de-risks by exiting the position in full.
            exit_quantity = min(proposal.quantity, held) if proposal.quantity > 0 else held
            if exit_quantity != proposal.quantity:
                proposal = replace(proposal, quantity=exit_quantity)
        pure_de_risking_sell = (
            proposal.side == Side.SELL and held > 0 and proposal.quantity <= held
        )
        stress = self.stress_agent.evaluate(proposal, portfolio)

        def refuse(reason: str) -> SwarmExecutionResult:
            rejected = self.warden.reject(reason, proposal, tenant_id)
            if lifecycle.state != OrderState.REJECTED:
                lifecycle.transition(OrderState.REJECTED)
            trace = self.xai_logger.log(request, weighted_evidence, proposal, stress, rejected)
            return SwarmExecutionResult(
                proposal, rejected, None, stress, trace, lifecycle.state
            )

        if self.pre_submit_check is not None:
            veto = self.pre_submit_check(proposal)
            if veto:
                return refuse(veto)
        # A halt freezes new risk. It must never trap a position: covered SELLs are allowed
        # through the same governed paper path while every risk-adding order stays blocked.
        if self.kill_switch.engaged and not pure_de_risking_sell:
            return refuse(f"kill_switch_engaged:{self.kill_switch.reason}")
        if proposal.side == Side.SELL and held <= 0:
            return refuse("paper_naked_sell_disabled")
        if proposal.side == Side.BUY and proposal.quantity <= 0:
            # The sizer found no risk budget, trade cap or deployable capital for an entry.
            return refuse("position_sizer_no_capacity")
        if not stress.passed and not pure_de_risking_sell:
            return refuse("STRESS_VETO")

        risk = self.warden.evaluate(
            proposal, plan, portfolio, country_exposure=country_exposure, tenant_id=tenant_id
        )
        if not risk.approved or risk.order is None:
            trace = self.xai_logger.log(request, weighted_evidence, proposal, stress, risk)
            lifecycle.transition(OrderState.REJECTED)
            return SwarmExecutionResult(proposal, risk, None, stress, trace, lifecycle.state)

        # C2: block a second entry into a symbol that is already open.
        if risk.order.side == Side.BUY and held > 0 and not self.allow_position_scaling:
            return refuse("position_already_open")
        if (
            risk.order.side == Side.BUY
            and held == 0
            and self.max_open_positions is not None
            and len(self.broker.get_positions(tenant_id)) >= self.max_open_positions
        ):
            return refuse("max_open_positions_reached")
        # C2 (extension): and do not immediately re-enter a symbol just stopped out.
        if risk.order.side == Side.BUY and self._in_exit_cooldown(
            risk.order, tenant_id, request.observed_at
        ):
            return refuse("re_entry_cooldown_active")
        lifecycle.transition(OrderState.RISK_APPROVED)

        # Prepare before execution. The canonical trace and replay guard must commit in
        # the same database transaction as the fill, cash, positions and fees.
        trace = self.xai_logger.build(request, weighted_evidence, proposal, stress, risk)
        fill_evidence = {
            **json.loads(self.xai_logger.to_json(trace)),
            "schema": "pramana.swarm_fill.v1", "event_type": "swarm_fill",
            "approved_order": self.xai_logger._normalize(asdict(risk.order)),
        }
        key = order_idempotency_key(risk.order, proposal.decision_id)

        lifecycle.transition(OrderState.SUBMITTED)
        try:
            fill = self.broker.submit_with_evidence(risk.order, fill_evidence, key.value)
        except PaperBrokerDatabaseLockedError:
            return refuse("paper_broker_database_locked")
        except ValueError as error:
            # C3: a broker-side rejection (insufficient cash/position) is a governed outcome,
            # not a daemon-killing exception.
            return refuse("duplicate_order" if str(error) == "duplicate_order" else f"broker_rejected:{error}")
        lifecycle.transition(OrderState.FILLED)
        trace = replace(trace, order_id=fill.order_id)
        try:
            self.xai_logger.record(trace)
        except OSError:
            # Files are an optional projection. The exact trace already exists in the
            # ledger; do not turn a committed fill into a misleading execution failure.
            LOGGER.exception("xai_file_projection_failed order_id=%s; canonical evidence retained in ledger", fill.order_id)
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

    def _held_quantity(self, order: OrderIntent | TradeProposal, tenant_id: str) -> int:
        return sum(
            position.quantity
            for position in self.broker.get_positions(tenant_id)
            if position.symbol == order.symbol
            and position.market == order.market
            and position.asset_class == order.asset_class
        )
