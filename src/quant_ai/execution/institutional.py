"""Institutional paper coordinator: evidence -> capital -> risk -> schedule -> OMS -> accounting.

This is intentionally paper-only. It owns no live broker transport. The coordinator makes
cross-module authority explicit and durable: each stage can refuse, and downstream stages
cannot infer an approval that was never produced upstream.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from quant_ai.accounting.trading import TradingAccounting
from quant_ai.agents.swarm import TradeProposal
from quant_ai.brokers.adapter import BrokerPosition
from quant_ai.decision.edge import CalibratedEdgeGate, EdgeDecision, EdgeEvidence
from quant_ai.domain.models import OrderIntent, PortfolioSnapshot, Side
from quant_ai.execution.derivative_margin import MARGINED_FUTURES_ASSET_CLASSES
from quant_ai.execution.paper_ledger import PaperBrokerDatabaseLockedError, PaperBrokerService
from quant_ai.execution.planner import (
    ExecutionAlgorithm,
    ExecutionConstraints,
    ExecutionPlan,
    ExecutionPlanner,
    VolumeBucket,
)
from quant_ai.execution.program import ExecutionProgram, ExecutionProgramJournal
from quant_ai.orders.intent import canonical_order_intent
from quant_ai.orders.oms import DurableOms
from quant_ai.orders.state import OrderState
from quant_ai.planning.capital import CapitalPlan
from quant_ai.portfolio.optimizer import (
    PortfolioOptimizationPolicy,
    PortfolioOptimizationResult,
    StrategyOpportunity,
    StrategyPortfolioOptimizer,
)
from quant_ai.risk.factor_liquidity import (
    FactorLiquidityFirewall,
    FactorLiquidityPolicy,
    FactorLiquidityPosition,
)
from quant_ai.risk.warden import RiskWarden

MARGINED_ASSETS = MARGINED_FUTURES_ASSET_CLASSES
TAX_CODES = frozenset({"STT", "CTT", "GST", "STAMP", "SEBI"})


class InstitutionalStage(str, Enum):
    EDGE = "EDGE"
    ALLOCATION = "ALLOCATION"
    FACTOR_LIQUIDITY = "FACTOR_LIQUIDITY"
    RISK = "RISK"
    EXECUTION_PLAN = "EXECUTION_PLAN"
    READY = "READY"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


@dataclass(frozen=True)
class InstitutionalTradeRequest:
    proposal: TradeProposal
    strategy_id: str
    edge_evidence: EdgeEvidence | None
    capital_plan: CapitalPlan
    portfolio: PortfolioSnapshot
    strategy_opportunities: tuple[StrategyOpportunity, ...]
    strategy_correlations: Mapping[tuple[str, str], Decimal]
    current_strategy_weights: Mapping[str, Decimal]
    allocation_policy: PortfolioOptimizationPolicy
    projected_factor_positions: tuple[FactorLiquidityPosition, ...]
    factor_policy: FactorLiquidityPolicy
    execution_algorithm: ExecutionAlgorithm
    execution_constraints: ExecutionConstraints
    volume_buckets: tuple[VolumeBucket, ...]
    tenant_id: str
    currency: str
    base_rate: Decimal
    observed_at: datetime

    def __post_init__(self) -> None:
        if not self.strategy_id.strip() or not self.tenant_id.strip():
            raise ValueError("institutional_trade_identity_required")
        if len(self.currency) != 3 or self.currency.upper() != self.currency:
            raise ValueError("institutional_trade_currency_invalid")
        if not self.base_rate.is_finite() or self.base_rate <= 0:
            raise ValueError("institutional_trade_base_rate_must_be_positive")
        instrument = getattr(self.proposal, "instrument", None)
        if instrument is not None and instrument.currency != self.currency:
            raise ValueError("institutional_contract_currency_mismatch")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("institutional_trade_observed_at_must_be_timezone_aware")


@dataclass(frozen=True)
class InstitutionalPreparation:
    approved: bool
    stage: InstitutionalStage
    reason: str
    program: ExecutionProgram | None = None
    edge: EdgeDecision | None = None
    allocation: PortfolioOptimizationResult | None = None
    execution_plan: ExecutionPlan | None = None
    approved_order: OrderIntent | None = None


@dataclass(frozen=True)
class InstitutionalExecutionResult:
    stage: InstitutionalStage
    program: ExecutionProgram
    executed_sequences: tuple[int, ...]
    reason: str


SnapshotProvider = Callable[[], PortfolioSnapshot]
FactorPositionProvider = Callable[[OrderIntent, PortfolioSnapshot], tuple[FactorLiquidityPosition, ...]]
StrategyExposureProvider = Callable[[str], Decimal]
SliceVolumeProvider = Callable[[ExecutionProgram, int, datetime], int]


def _stable(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {
            str(key): _stable(item)
            for key, item in sorted(value.items(), key=lambda row: str(row[0]))
        }
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_stable(item) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        return _stable(asdict(value))
    return value


def request_fingerprint(request: InstitutionalTradeRequest) -> str:
    payload = {
        "proposal": request.proposal,
        "strategy_id": request.strategy_id,
        "edge_evidence": request.edge_evidence,
        "capital_plan": request.capital_plan,
        "portfolio": request.portfolio,
        "strategy_opportunities": request.strategy_opportunities,
        "strategy_correlations": tuple(
            (left, right, value)
            for (left, right), value in sorted(request.strategy_correlations.items())
        ),
        "current_strategy_weights": request.current_strategy_weights,
        "allocation_policy": request.allocation_policy,
        "projected_factor_positions": request.projected_factor_positions,
        "factor_policy": request.factor_policy,
        "execution_algorithm": request.execution_algorithm,
        "execution_constraints": request.execution_constraints,
        "volume_buckets": request.volume_buckets,
        "tenant_id": request.tenant_id,
        "currency": request.currency,
        "base_rate": request.base_rate,
        "observed_at": request.observed_at,
    }
    return hashlib.sha256(
        json.dumps(_stable(payload), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class InstitutionalPaperCoordinator:
    def __init__(
        self,
        *,
        broker: PaperBrokerService,
        oms: DurableOms,
        programs: ExecutionProgramJournal,
        accounting: TradingAccounting,
        warden: RiskWarden,
        snapshot_provider: SnapshotProvider,
        factor_position_provider: FactorPositionProvider,
        strategy_exposure_provider: StrategyExposureProvider,
        slice_volume_provider: SliceVolumeProvider,
        edge_gate: CalibratedEdgeGate | None = None,
        optimizer: StrategyPortfolioOptimizer | None = None,
        execution_planner: ExecutionPlanner | None = None,
    ) -> None:
        self.broker = broker
        self.oms = oms
        self.programs = programs
        self.accounting = accounting
        self.warden = warden
        self.snapshot_provider = snapshot_provider
        self.factor_position_provider = factor_position_provider
        self.strategy_exposure_provider = strategy_exposure_provider
        self.slice_volume_provider = slice_volume_provider
        self.edge_gate = edge_gate or CalibratedEdgeGate()
        self.optimizer = optimizer or StrategyPortfolioOptimizer()
        self.execution_planner = execution_planner or ExecutionPlanner()
        self._requests: dict[str, InstitutionalTradeRequest] = {}
        self._orders: dict[str, OrderIntent] = {}

    def prepare(self, request: InstitutionalTradeRequest) -> InstitutionalPreparation:
        proposal = request.proposal
        held = request.portfolio.symbol_quantity.get(proposal.symbol, 0)
        de_risking = (
            proposal.side is Side.SELL and held > 0 and proposal.quantity <= held
        )
        edge: EdgeDecision | None = None
        allocation: PortfolioOptimizationResult | None = None
        if not de_risking:
            if request.edge_evidence is None:
                return self._reject(InstitutionalStage.EDGE, "edge_evidence_required")
            edge = self.edge_gate.evaluate(request.edge_evidence)
            if not edge.approved:
                return InstitutionalPreparation(
                    False, InstitutionalStage.EDGE, ";".join(edge.reasons), edge=edge
                )
            allocation = self.optimizer.optimize(
                request.strategy_opportunities,
                correlations=request.strategy_correlations,
                current_weights=request.current_strategy_weights,
                policy=request.allocation_policy,
            )
            strategy_weight = allocation.weights().get(request.strategy_id, Decimal(0))
            if strategy_weight <= 0:
                return InstitutionalPreparation(
                    False, InstitutionalStage.ALLOCATION, "strategy_not_allocated",
                    edge=edge, allocation=allocation,
                )
            max_notional = request.portfolio.equity * strategy_weight
            if proposal.reference_price * proposal.quantity > max_notional:
                return InstitutionalPreparation(
                    False, InstitutionalStage.ALLOCATION, "strategy_allocation_notional_limit",
                    edge=edge, allocation=allocation,
                )
            factor = FactorLiquidityFirewall(request.factor_policy).evaluate(
                request.projected_factor_positions, equity=request.portfolio.equity
            )
            if not factor.approved:
                return InstitutionalPreparation(
                    False, InstitutionalStage.FACTOR_LIQUIDITY, factor.reason,
                    edge=edge, allocation=allocation,
                )
        risk = self.warden.evaluate(
            proposal, request.capital_plan, request.portfolio,
            tenant_id=request.tenant_id, now=request.observed_at,
        )
        if not risk.approved or risk.order is None:
            return InstitutionalPreparation(
                False, InstitutionalStage.RISK, risk.reason, edge=edge, allocation=allocation
            )
        plan = self.execution_planner.plan(
            risk.order,
            algorithm=request.execution_algorithm,
            constraints=request.execution_constraints,
            buckets=request.volume_buckets,
            source="institutional coordinator supplied liquidity schedule",
        )
        program_id = "PROGRAM-" + hashlib.sha256(
            f"{request.tenant_id}|{proposal.decision_id}|{plan.algorithm.value}".encode()
        ).hexdigest()[:32]
        parent_order = replace(risk.order, strategy_id=request.strategy_id)
        program = self.programs.create(
            program_id=program_id,
            tenant_id=request.tenant_id,
            decision_id=proposal.decision_id,
            symbol=proposal.symbol,
            plan=plan,
            runtime_context_sha256=request_fingerprint(request),
            created_at=request.observed_at,
            parent_order_payload=canonical_order_intent(parent_order),
        )
        self._requests[program.program_id] = request
        self._orders[program.program_id] = parent_order
        return InstitutionalPreparation(
            approved=True,
            stage=InstitutionalStage.READY,
            reason="ready",
            program=program,
            edge=edge,
            allocation=allocation,
            execution_plan=plan,
            approved_order=self._orders[program.program_id],
        )

    @staticmethod
    def _reject(stage: InstitutionalStage, reason: str) -> InstitutionalPreparation:
        return InstitutionalPreparation(False, stage, reason)

    def execute_due(self, program_id: str, *, now: datetime) -> InstitutionalExecutionResult:
        request = self._requests.get(program_id)
        parent = self._orders.get(program_id)
        if request is None or parent is None:
            raise ValueError("execution_program_runtime_context_unavailable_after_restart")
        executed: list[int] = []
        program = self.programs.get(program_id)
        for slice_ in self.programs.due(program_id, now):
            current_snapshot = self.snapshot_provider()
            child = replace(parent, quantity=slice_.quantity)
            child_proposal = replace(request.proposal, quantity=slice_.quantity)
            held = current_snapshot.symbol_quantity.get(child.symbol, 0)
            de_risking = child.side is Side.SELL and held > 0 and child.quantity <= held
            if not de_risking:
                allocation = self.optimizer.optimize(
                    request.strategy_opportunities,
                    correlations=request.strategy_correlations,
                    current_weights=request.current_strategy_weights,
                    policy=request.allocation_policy,
                )
                strategy_weight = allocation.weights().get(request.strategy_id, Decimal(0))
                current_strategy_exposure = self.strategy_exposure_provider(request.strategy_id)
                if (
                    strategy_weight <= 0
                    or current_strategy_exposure + child.reference_price * child.quantity
                    > current_snapshot.equity * strategy_weight
                ):
                    reason = "strategy_allocation_notional_limit_at_slice"
                    self.programs.mark_failed(program_id, slice_.sequence, reason)
                    return InstitutionalExecutionResult(
                        InstitutionalStage.FAILED, self.programs.get(program_id),
                        tuple(executed), reason,
                    )
                live_volume = self.slice_volume_provider(program, slice_.sequence, now)
                try:
                    self.execution_planner.plan(
                        child,
                        algorithm=ExecutionAlgorithm.IMMEDIATE,
                        constraints=request.execution_constraints,
                        buckets=(VolumeBucket(now, live_volume),),
                        source="live slice liquidity recheck",
                    )
                except (TypeError, ValueError) as error:
                    reason = f"execution_liquidity_recheck_failed:{error}"
                    self.programs.mark_failed(program_id, slice_.sequence, reason)
                    return InstitutionalExecutionResult(
                        InstitutionalStage.FAILED, self.programs.get(program_id),
                        tuple(executed), reason,
                    )
                factor_positions = self.factor_position_provider(child, current_snapshot)
                factor = FactorLiquidityFirewall(request.factor_policy).evaluate(
                    factor_positions, equity=current_snapshot.equity
                )
                if not factor.approved:
                    self.programs.mark_failed(program_id, slice_.sequence, factor.reason)
                    return InstitutionalExecutionResult(
                        InstitutionalStage.FAILED, self.programs.get(program_id),
                        tuple(executed), factor.reason,
                    )
            risk = self.warden.evaluate(
                child_proposal, request.capital_plan, current_snapshot,
                tenant_id=request.tenant_id, now=now,
            )
            if not risk.approved or risk.order is None:
                self.programs.mark_failed(program_id, slice_.sequence, risk.reason)
                return InstitutionalExecutionResult(
                    InstitutionalStage.FAILED, self.programs.get(program_id),
                    tuple(executed), risk.reason,
                )
            approved_child = replace(risk.order, strategy_id=request.strategy_id)
            if canonical_order_intent(approved_child) != canonical_order_intent(child):
                reason = "execution_child_approval_identity_mismatch"
                self.programs.mark_failed(program_id, slice_.sequence, reason)
                return InstitutionalExecutionResult(InstitutionalStage.FAILED,
                    self.programs.get(program_id), tuple(executed), reason)
            child = approved_child
            before = self._position(child)
            decision_id = f"{request.proposal.decision_id}:slice:{slice_.sequence}"
            oms_row = self.oms.create(child, decision_id=decision_id, now=now)
            if oms_row.state is not OrderState.CREATED:
                self.programs.mark_failed(program_id, slice_.sequence, "child_order_not_new")
                return InstitutionalExecutionResult(
                    InstitutionalStage.FAILED, self.programs.get(program_id),
                    tuple(executed), "child_order_not_new",
                )
            self.oms.approve_risk(oms_row.client_order_id, now=now)
            self.oms.submitted(oms_row.client_order_id, now=now)
            evidence = {
                "schema": "pramana.swarm_fill.v1",
                "event_type": "swarm_fill",
                "institutional_program": program_id,
                "institutional_slice": slice_.sequence,
            }
            try:
                fill = self.broker.submit_with_evidence(
                    child, evidence, f"{program_id}:{slice_.sequence}"
                )
            except (PaperBrokerDatabaseLockedError, ValueError) as error:
                reason = f"paper_broker_rejected:{error}"
                self.oms.reject(oms_row.client_order_id, reason=reason, now=now)
                self.programs.mark_failed(program_id, slice_.sequence, reason)
                return InstitutionalExecutionResult(
                    InstitutionalStage.FAILED, self.programs.get(program_id),
                    tuple(executed), reason,
                )
            self.oms.fill(
                oms_row.client_order_id, fill_id=fill.order_id,
                quantity=fill.filled_quantity, price=fill.average_price,
                broker_order_id=fill.order_id, now=now,
            )
            # The paper fill and OMS are committed before the accounting database can be.
            # Persist this recovery state first so a crash cannot lead to resubmission.
            self.programs.mark_filled_unaccounted(
                program_id, slice_.sequence,
                client_order_id=oms_row.client_order_id,
                broker_order_id=fill.order_id,
                pre_fill_average_price=(
                    None if before is None else str(before.average_price)
                ),
            )
            try:
                self._post_accounting(
                    child, fill.order_id, fill.average_price, before, request
                )
            except (KeyError, TypeError, ValueError) as error:
                return InstitutionalExecutionResult(
                    InstitutionalStage.FAILED, self.programs.get(program_id),
                    tuple(executed), f"accounting_reconciliation_required:{error}",
                )
            self.programs.mark_executed(
                program_id, slice_.sequence,
                client_order_id=oms_row.client_order_id,
                broker_order_id=fill.order_id,
            )
            executed.append(slice_.sequence)
        program = self.programs.get(program_id)
        return InstitutionalExecutionResult(
            InstitutionalStage.COMPLETE if program.state.value == "COMPLETE" else InstitutionalStage.READY,
            program,
            tuple(executed),
            "complete" if program.state.value == "COMPLETE" else "awaiting_slices",
        )

    def bind_runtime_context(
        self,
        program_id: str,
        *,
        request: InstitutionalTradeRequest,
        parent_order: OrderIntent,
    ) -> None:
        """Rebind exact runtime inputs after restart without resubmitting any slice."""
        program = self.programs.get(program_id)
        if (
            program.tenant_id != request.tenant_id
            or program.decision_id != request.proposal.decision_id
            or program.symbol != request.proposal.symbol
            or program.parent_quantity != parent_order.quantity
            or parent_order.tenant_id != request.tenant_id
            or parent_order.symbol != program.symbol
            or request_fingerprint(request) != program.runtime_context_sha256
            or program.parent_order_payload is None
            or canonical_order_intent(parent_order) != program.parent_order_payload
        ):
            raise ValueError("execution_program_runtime_context_mismatch")
        self._requests[program_id] = request
        self._orders[program_id] = parent_order

    def reconcile_accounting(self, program_id: str) -> InstitutionalExecutionResult:
        request = self._requests.get(program_id)
        parent = self._orders.get(program_id)
        if request is None or parent is None:
            raise ValueError("execution_program_runtime_context_unavailable_after_restart")
        finalized: list[int] = []
        for slice_ in self.programs.unaccounted(program_id):
            if slice_.broker_order_id is None or slice_.client_order_id is None:
                raise ValueError("unaccounted_slice_missing_execution_identity")
            ledger = next(
                (
                    item for item in self.broker.ledger_entries(request.tenant_id)
                    if item.order_id == slice_.broker_order_id
                ),
                None,
            )
            if ledger is None:
                raise ValueError("unaccounted_slice_broker_fill_missing")
            child = replace(parent, quantity=slice_.quantity)
            before = None
            if child.side is Side.SELL:
                if slice_.pre_fill_average_price is None:
                    raise ValueError("sell_accounting_recovery_cost_basis_missing")
                before = BrokerPosition(
                    request.tenant_id, child.symbol, child.market, child.asset_class,
                    child.quantity, Decimal(slice_.pre_fill_average_price),
                    child.stop_price, child.take_profit_price,
                )
            self._post_accounting(
                child, ledger.order_id, ledger.fill_price, before, request
            )
            self.programs.mark_executed(
                program_id, slice_.sequence,
                client_order_id=slice_.client_order_id,
                broker_order_id=slice_.broker_order_id,
            )
            finalized.append(slice_.sequence)
        program = self.programs.get(program_id)
        return InstitutionalExecutionResult(
            InstitutionalStage.COMPLETE if program.state.value == "COMPLETE" else InstitutionalStage.READY,
            program, tuple(finalized),
            "complete" if program.state.value == "COMPLETE" else "awaiting_slices",
        )

    def _position(self, order: OrderIntent) -> BrokerPosition | None:
        return next(
            (
                item for item in self.broker.get_positions(order.tenant_id)
                if item.symbol == order.symbol
                and item.market == order.market
                and item.asset_class == order.asset_class
            ),
            None,
        )

    def _post_accounting(
        self,
        order: OrderIntent,
        broker_order_id: str,
        fill_price: Decimal,
        before: BrokerPosition | None,
        request: InstitutionalTradeRequest,
    ) -> None:
        ledger = next(
            item for item in self.broker.ledger_entries(order.tenant_id)
            if item.order_id == broker_order_id
        )
        rate = request.base_rate
        reference = f"paper fill {broker_order_id} {order.symbol}"
        if order.asset_class in MARGINED_ASSETS:
            if ledger.margin_change is None:
                raise ValueError("derivative_fill_missing_margin_change")
            if ledger.margin_change > 0:
                self.accounting.reserve_margin(
                    f"margin:{broker_order_id}", currency=request.currency,
                    amount=ledger.margin_change, base_amount=ledger.margin_change * rate,
                    reference=reference, at=ledger.created_at,
                )
            elif ledger.margin_change < 0:
                released = -ledger.margin_change
                self.accounting.release_margin(
                    f"margin:{broker_order_id}", currency=request.currency,
                    amount=released, base_amount=released * rate,
                    reference=reference, at=ledger.created_at,
                )
            if order.side is Side.SELL:
                if before is None or before.quantity < order.quantity:
                    raise ValueError("derivative_close_cost_basis_unavailable")
                pnl = (fill_price - before.average_price) * Decimal(order.quantity)
                if pnl != 0:
                    self.accounting.realize_pnl(
                        f"pnl:{broker_order_id}", currency=request.currency,
                        pnl=pnl, base_pnl=pnl * rate, reference=reference,
                        at=ledger.created_at,
                    )
        else:
            notional = fill_price * Decimal(order.quantity)
            if order.side is Side.BUY:
                self.accounting.buy_security(
                    f"trade:{broker_order_id}", currency=request.currency,
                    notional=notional, base_notional=notional * rate,
                    reference=reference, at=ledger.created_at,
                )
            else:
                if before is None or before.quantity < order.quantity:
                    raise ValueError("security_sale_cost_basis_unavailable")
                released = before.average_price * Decimal(order.quantity)
                self.accounting.sell_security(
                    f"trade:{broker_order_id}", currency=request.currency,
                    proceeds=notional, released_cost=released,
                    base_proceeds=notional * rate, base_released_cost=released * rate,
                    reference=reference, at=ledger.created_at,
                )
        for cost in self.broker.cost_entries(order.tenant_id):
            if cost.order_id != broker_order_id or not cost.cash_debit or cost.amount == 0:
                continue
            self.accounting.cash_fee(
                f"cost:{broker_order_id}:{cost.code}", currency=request.currency,
                amount=cost.amount, base_amount=cost.amount * rate,
                reference=f"{reference} cost {cost.code}", tax=cost.code in TAX_CODES,
                at=cost.created_at,
            )
