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
from pathlib import Path

from quant_ai.accounting.protective import ProtectiveAccountingReport, ProtectiveExitAccounting
from quant_ai.accounting.trading import TradingAccounting
from quant_ai.agents.swarm import TradeProposal
from quant_ai.brokers.adapter import BrokerPosition
from quant_ai.decision.edge import CalibratedEdgeGate, EdgeDecision, EdgeEvidence
from quant_ai.domain.models import AssetClass, OrderIntent, PortfolioSnapshot, Side
from quant_ai.execution.derivative_margin import MARGINED_FUTURES_ASSET_CLASSES
from quant_ai.execution.paper_ledger import PaperBrokerDatabaseLockedError, PaperBrokerService
from quant_ai.execution.planner import (
    ExecutionAlgorithm,
    ExecutionConstraints,
    ExecutionPlan,
    ExecutionPlanner,
    VolumeBucket,
)
from quant_ai.execution.program import (
    ExecutionProgram,
    ExecutionProgramJournal,
    ProgramState,
    SliceState,
)
from quant_ai.execution.request_context import ExecutionContextError, decode_context, encode_context
from quant_ai.execution.risk_authority import (
    authority_digest,
    bound_policy,
    build_authority,
    parse_authority,
    policy_values,
)
from quant_ai.execution.shared_risk import (
    SharedRiskError,
    SharedRiskPolicy,
    SharedRiskReservations,
    selected,
)
from quant_ai.execution.shared_risk_binding import pin_account, read_binding, verify_binding_pair
from quant_ai.orders.intent import bound_identity, canonical_order_intent, order_from_snapshot
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
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
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



def _nonnegative_amount(value) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and value >= 0


def _parent_edge_risk_issue(parent, evidence, edge, equity) -> str | None:
    """The full approved parent's modeled loss, never a fresh allowance per slice.

    Conservative proxy: the larger of stop-distance loss and empirical adverse
    payoff plus declared costs. This is not a guaranteed maximum realized loss.
    Rechecking uses current equity without subtracting previously executed slices.
    """
    if (evidence is None or not _nonnegative_amount(equity) or equity <= 0
            or type(parent.quantity) is not int or not 0 < parent.quantity <= 2**53 - 1
            or not _nonnegative_amount(parent.reference_price) or parent.reference_price <= 0
            or not _nonnegative_amount(parent.stop_price) or parent.stop_price <= 0
            or not _nonnegative_amount(edge.recommended_risk_fraction)
            or not 0 < edge.recommended_risk_fraction <= 1):
        return "edge_planned_risk_unavailable"
    stop_loss = abs(parent.reference_price - parent.stop_price) * parent.quantity
    adverse_loss = (parent.reference_price * parent.quantity
                    * (evidence.average_loss_return + evidence.expected_cost_return))
    if max(stop_loss, adverse_loss) > equity * edge.recommended_risk_fraction:
        return "edge_planned_risk_limit"
    return None


def _factor_book_projection_issue(order, snapshot, positions) -> str | None:
    """Bind supplied factor inputs to every held symbol plus the proposed change.

    Quantities and marked exposure come from the supplied portfolio snapshot, not
    from the factor provider's own totals. This long-only book does not infer shorts,
    missing marks or omitted holdings. Source authentication remains separate.
    """
    try:
        quantities, exposures = snapshot.symbol_quantity, snapshot.symbol_exposure
        if not isinstance(quantities, Mapping) or not isinstance(exposures, Mapping):
            raise TypeError("snapshot_maps_invalid")
        if set(quantities) != set(exposures):
            raise ValueError("snapshot_coverage_invalid")
        expected = {}
        for symbol, quantity in quantities.items():
            value = exposures[symbol]
            if (not isinstance(symbol, str) or not symbol.strip()
                    or type(quantity) is not int or not 0 <= quantity <= 2**53 - 1
                    or not _nonnegative_amount(value) or (quantity == 0) != (value == 0)):
                raise ValueError("snapshot_position_invalid")
            if quantity:
                expected[symbol] = (quantity, value)
        if (not _nonnegative_amount(snapshot.gross_exposure)
                or sum(exposures.values(), Decimal(0)) != snapshot.gross_exposure):
            raise ValueError("snapshot_gross_mismatch")
        if (order.side is not Side.BUY or type(order.quantity) is not int
                or not 0 < order.quantity <= 2**53 - 1
                or not _nonnegative_amount(order.reference_price) or order.reference_price <= 0):
            raise ValueError("unsupported_adding_order")
        held, value = expected.get(order.symbol, (0, Decimal(0)))
        expected[order.symbol] = (held + order.quantity,
                                  value + order.reference_price * order.quantity)
        if not isinstance(positions, (tuple, list)):
            raise TypeError("positions_invalid")
        observed = {}
        for position in positions:
            if not isinstance(position, FactorLiquidityPosition):
                raise TypeError("position_type_invalid")
            # Revalidate mutable nested inputs before the firewall consumes them.
            replace(position)
            if position.symbol in observed:
                raise ValueError("duplicate_symbol")
            observed[position.symbol] = (position.quantity, position.market_value)
        if observed != expected:
            raise ValueError("positions_do_not_match_projected_book")
    except (TypeError, ValueError, ArithmeticError, AttributeError) as error:
        return f"factor_book_projection_mismatch:{error}"
    return None


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
        shared_risk_policy: SharedRiskPolicy | None = None,
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
        self.shared_risk_policy = shared_risk_policy
        self.shared_risk = SharedRiskReservations(programs)
        self._requests: dict[str, InstitutionalTradeRequest] = {}
        self._orders: dict[str, OrderIntent] = {}
        self._source_requests: dict[str, InstitutionalTradeRequest] = {}

    def prepare(self, request: InstitutionalTradeRequest) -> InstitutionalPreparation:
        original_request = request
        initial_request_digest = request_fingerprint(request)
        proposal = request.proposal
        held = request.portfolio.symbol_quantity.get(proposal.symbol, 0)
        de_risking = (
            proposal.side is Side.SELL and held > 0 and proposal.quantity <= held
        )
        approved_policy = None
        edge: EdgeDecision | None = None
        allocation: PortfolioOptimizationResult | None = None
        approved_shared_policy = None
        if not de_risking:
            try:
                if self.shared_risk_policy is not None:
                    if not isinstance(self.shared_risk_policy, SharedRiskPolicy):
                        raise SharedRiskError("shared_risk_configuration_required")
                    approved_shared_policy = replace(self.shared_risk_policy)
            except (TypeError, ValueError, ArithmeticError):
                return self._reject(InstitutionalStage.RISK, "shared_risk_configuration_invalid")
            if request.edge_evidence is None:
                return self._reject(InstitutionalStage.EDGE, "edge_evidence_required")
            try:
                values = policy_values(self.edge_gate.policy)
                approved_policy = replace(self.edge_gate.policy)
                if values != policy_values(approved_policy):
                    raise ValueError("execution_risk_policy_changed")
            except (TypeError, ValueError, ArithmeticError, AttributeError):
                return self._reject(InstitutionalStage.EDGE, "execution_risk_policy_unavailable")
            edge = CalibratedEdgeGate(approved_policy).evaluate(request.edge_evidence)
            if not edge.approved:
                return InstitutionalPreparation(
                    False, InstitutionalStage.EDGE, ";".join(edge.reasons), edge=edge
                )
            issue = _parent_edge_risk_issue(proposal, request.edge_evidence, edge,
                                            request.portfolio.equity)
            if issue:
                return InstitutionalPreparation(False, InstitutionalStage.EDGE, issue, edge=edge)
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
            current_exposure = self._strategy_exposure(request.strategy_id)
            if current_exposure is None:
                return InstitutionalPreparation(False, InstitutionalStage.ALLOCATION,
                    "strategy_exposure_measure_unavailable", edge=edge, allocation=allocation)
            max_notional = request.portfolio.equity * strategy_weight
            if current_exposure + proposal.reference_price * proposal.quantity > max_notional:
                return InstitutionalPreparation(
                    False, InstitutionalStage.ALLOCATION, "strategy_allocation_notional_limit",
                    edge=edge, allocation=allocation,
                )
            issue = _factor_book_projection_issue(proposal, request.portfolio,
                                                   request.projected_factor_positions)
            if issue:
                return InstitutionalPreparation(False, InstitutionalStage.FACTOR_LIQUIDITY,
                    issue, edge=edge, allocation=allocation)
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
        if not de_risking:
            try:
                changed = policy_values(self.edge_gate.policy) != policy_values(approved_policy)
            except (TypeError, ValueError, ArithmeticError, AttributeError):
                changed = True
            if changed:
                return self._reject(InstitutionalStage.EDGE, "execution_risk_policy_changed")
        parent_payload = canonical_order_intent(parent_order)
        authority = build_authority(program_id=program_id, tenant_id=request.tenant_id,
            request_sha256=request_fingerprint(request), parent_payload=parent_payload,
            policy=approved_policy)
        if request_fingerprint(request) != initial_request_digest:
            return self._reject(InstitutionalStage.EXECUTION_PLAN, "execution_context_request_changed")
        try:
            context_payload = encode_context(request, plan)
            # Separate both caller-owned and returned objects from the inputs bound
            # in memory. The persisted payload is immutable and can be decoded afresh.
            saved = decode_context(context_payload)
            request = saved.request
        except ExecutionContextError as error:
            return self._reject(InstitutionalStage.EXECUTION_PLAN, str(error))
        program_args = {"program_id": program_id, "tenant_id": request.tenant_id,
            "decision_id": proposal.decision_id, "symbol": proposal.symbol, "plan": plan,
            "runtime_context_sha256": authority_digest(authority), "created_at": request.observed_at,
            "parent_order_payload": parent_payload, "risk_authority_payload": authority,
            "context_payload": context_payload}
        if de_risking:
            program = self.programs.create(**program_args)
        else:
            try:
                # Capacity and the parent/slices commit together in this one journal.
                with self.programs.transaction():
                    current_shared = (replace(self.shared_risk_policy)
                        if isinstance(self.shared_risk_policy, SharedRiskPolicy) else self.shared_risk_policy)
                    if current_shared != approved_shared_policy:
                        raise SharedRiskError("shared_risk_configuration_changed")
                    with self.broker._lock:
                        broker_pin = read_binding(self.broker._connection, request.tenant_id)
                        if broker_pin is not None:
                            verify_binding_pair(self.broker._connection, self.programs.db, request.tenant_id)
                    configured = selected(self.programs.db, request.tenant_id)
                    shared = configured or self.shared_risk_policy is not None or broker_pin is not None
                    if shared:
                        if (request.currency not in {"INR", "USD"} or request.base_rate != 1
                                or proposal.asset_class not in {AssetClass.EQUITY, AssetClass.ETF}):
                            raise SharedRiskError("shared_risk_single_currency_cash_required")
                        self.shared_risk.bind(self.shared_risk_policy, tenant_id=request.tenant_id,
                            ledger_key=self._shared_ledger_key(request.tenant_id),
                            empty_ledger=not self.broker.ledger_entries(request.tenant_id))
                    program = self.programs.create(**program_args)
                    if shared:
                        self.shared_risk.reserve(program, evidence=request.edge_evidence, equity=request.portfolio.equity,
                            currency=request.currency, at=request.observed_at)
                        pin_account(self.broker, self.programs.db, request.tenant_id,
                                    allow_create=not configured, program_id=program.program_id)
            except SharedRiskError as error:
                return self._reject(InstitutionalStage.RISK, str(error))
        self._source_requests[program.program_id] = original_request
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

    def _shared_ledger_key(self, tenant_id: str) -> str:
        with self.broker._lock:
            files = self.broker._connection.execute("PRAGMA database_list").fetchall()
        filename = next((row[2] for row in files if row[1] == "main"), "")
        if not filename or str(self.programs.path) == ":memory:":
            raise SharedRiskError("shared_risk_durable_storage_required")
        return hashlib.sha256(json.dumps([str(Path(filename).resolve()),
            str(self.programs.path.resolve()), tenant_id]).encode()).hexdigest()

    def _shared_risk_issue(self, program, request, equity) -> str | None:
        try:
            with self.broker._lock:
                pin = read_binding(self.broker._connection, request.tenant_id)
                if pin is not None:
                    verify_binding_pair(self.broker._connection, self.programs.db, request.tenant_id)
            if not selected(self.programs.db, request.tenant_id) and self.shared_risk_policy is None and pin is None:
                return None
            if request.base_rate != 1:
                return "shared_risk_single_currency_cash_required"
            self.shared_risk.check(program, policy=self.shared_risk_policy,
                ledger_key=self._shared_ledger_key(request.tenant_id), currency=request.currency, equity=equity,
                evidence=request.edge_evidence)
        except (TypeError, ValueError, ArithmeticError, AttributeError) as error:
            return str(error) if isinstance(error, SharedRiskError) else "shared_risk_measure_unavailable"
        return None

    def _policy_issue(self, program, parent) -> str | None:
        # Covered sales retain normal Warden checks without acquiring entry-only authority.
        if parent.side is Side.SELL:
            return None
        if program.risk_authority_version != 1:
            return "execution_risk_policy_binding_missing"
        try:
            saved = bound_policy(program.risk_authority_payload)
            if saved is None or policy_values(saved) != policy_values(self.edge_gate.policy):
                return "execution_risk_policy_changed"
        except (TypeError, ValueError, ArithmeticError, AttributeError):
            return "execution_risk_policy_unavailable"
        return None

    @staticmethod
    def _bound_request_digest(program) -> str:
        if program.risk_authority_version == 1:
            return parse_authority(program.risk_authority_payload)["requestSha256"]
        return program.runtime_context_sha256

    def _strategy_exposure(self, strategy_id: str) -> Decimal | None:
        try:
            value = self.strategy_exposure_provider(strategy_id)
        except (TypeError, ValueError, ArithmeticError, OSError, RuntimeError):
            return None
        return value if _nonnegative_amount(value) else None

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
        self._assert_runtime_intent(program, request, parent)
        if program.state in {ProgramState.FAILED, ProgramState.CANCELLED}:
            return InstitutionalExecutionResult(InstitutionalStage.FAILED, program, (), "program_terminal")
        if self.programs.recovery_required(request.tenant_id):
            return self._recovery_result(program_id, (), "pending_execution_or_accounting_recovery")
        for slice_ in self.programs.due(program_id, now):
            policy_issue = self._policy_issue(program, parent)
            if policy_issue:
                return self._recovery_result(program_id, tuple(executed), policy_issue)
            accounting_status = self.reconcile_protective_accounting(currency=request.currency)
            if accounting_status.status not in {"matched", "not_required"}:
                return self._recovery_result(program_id, tuple(executed), accounting_status.reason)
            child = replace(parent, quantity=slice_.quantity)
            decision_id = f"{request.proposal.decision_id}:slice:{slice_.sequence}"
            client_id = self.oms.client_order_id(child, decision_id)
            if not self.programs.claim_slice(program_id, slice_.sequence, client_order_id=client_id):
                return self._recovery_result(program_id, tuple(executed), "execution_slice_already_claimed")
            # Claim first, then obtain fresh risk inputs. A crash never makes this slice new again.
            current_snapshot = self.snapshot_provider()
            child_proposal = replace(request.proposal, quantity=slice_.quantity)
            held = current_snapshot.symbol_quantity.get(child.symbol, 0)
            de_risking = child.side is Side.SELL and held > 0 and child.quantity <= held
            if not de_risking:
                shared_issue = self._shared_risk_issue(program, request, current_snapshot.equity)
                if shared_issue:
                    self.programs.mark_failed(program_id, slice_.sequence, shared_issue)
                    return InstitutionalExecutionResult(InstitutionalStage.FAILED,
                        self.programs.get(program_id), tuple(executed), shared_issue)
                if request.edge_evidence is None:
                    edge_issue = "edge_evidence_required_at_slice"
                else:
                    saved_policy = (bound_policy(program.risk_authority_payload)
                                    if program.risk_authority_version == 1 else None)
                    if saved_policy is None:
                        reason = "execution_risk_policy_binding_missing"
                        self.programs.mark_failed(program_id, slice_.sequence, reason)
                        return InstitutionalExecutionResult(InstitutionalStage.FAILED,
                            self.programs.get(program_id), tuple(executed), reason)
                    edge = CalibratedEdgeGate(saved_policy).evaluate(request.edge_evidence)
                    edge_issue = ("edge_recheck_failed:" + ";".join(edge.reasons)
                                  if not edge.approved else
                                  _parent_edge_risk_issue(parent, request.edge_evidence, edge,
                                                         current_snapshot.equity))
                    if edge_issue in {"edge_planned_risk_limit", "edge_planned_risk_unavailable"}:
                        edge_issue += "_at_slice"
                if edge_issue:
                    self.programs.mark_failed(program_id, slice_.sequence, edge_issue)
                    return InstitutionalExecutionResult(InstitutionalStage.FAILED,
                        self.programs.get(program_id), tuple(executed), edge_issue)
                allocation = self.optimizer.optimize(
                    request.strategy_opportunities,
                    correlations=request.strategy_correlations,
                    current_weights=request.current_strategy_weights,
                    policy=request.allocation_policy,
                )
                strategy_weight = allocation.weights().get(request.strategy_id, Decimal(0))
                current_strategy_exposure = self._strategy_exposure(request.strategy_id)
                if current_strategy_exposure is None:
                    reason = "strategy_exposure_measure_unavailable"
                    self.programs.mark_failed(program_id, slice_.sequence, reason)
                    return InstitutionalExecutionResult(InstitutionalStage.FAILED,
                        self.programs.get(program_id), tuple(executed), reason)
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
                try:
                    factor_positions = self.factor_position_provider(child, current_snapshot)
                    issue = _factor_book_projection_issue(child, current_snapshot, factor_positions)
                except (TypeError, ValueError, ArithmeticError, AttributeError, OSError, RuntimeError):
                    issue = "factor_book_projection_mismatch:provider_unavailable"
                if issue:
                    self.programs.mark_failed(program_id, slice_.sequence, issue)
                    return InstitutionalExecutionResult(InstitutionalStage.FAILED,
                        self.programs.get(program_id), tuple(executed), issue)
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
            self._assert_runtime_intent(self.programs.get(program_id), request, parent)
            policy_issue = self._policy_issue(program, parent)
            if policy_issue:
                self.programs.mark_failed(program_id, slice_.sequence, policy_issue)
                return InstitutionalExecutionResult(InstitutionalStage.FAILED,
                    self.programs.get(program_id), tuple(executed), policy_issue)
            child = approved_child
            if not de_risking:
                shared_issue = self._shared_risk_issue(program, request, current_snapshot.equity)
                if shared_issue:
                    self.programs.mark_failed(program_id, slice_.sequence, shared_issue)
                    return InstitutionalExecutionResult(InstitutionalStage.FAILED,
                        self.programs.get(program_id), tuple(executed), shared_issue)
            before = self._position(child)
            decision_id = f"{request.proposal.decision_id}:slice:{slice_.sequence}"
            oms_row = self.oms.create(child, decision_id=decision_id, now=now)
            if oms_row.state is not OrderState.CREATED:
                return self._recovery_result(program_id, tuple(executed), "child_order_requires_reconciliation")
            self.oms.approve_risk(oms_row.client_order_id, now=now)
            self.oms.submitted(oms_row.client_order_id, now=now)
            evidence = {
                "schema": "pramana.swarm_fill.v1",
                "event_type": "swarm_fill",
                "institutional_program": program_id,
                "institutional_slice": slice_.sequence,
                "institutional_risk_authority_sha256": (program.runtime_context_sha256
                    if program.risk_authority_version == 1 else None),
                "order_intent_sha256": hashlib.sha256(canonical_order_intent(child).encode()).hexdigest(),
                "instrument_identity": bound_identity(child),
            }
            try:
                fill = self.broker.submit_with_evidence(
                    child, evidence, f"{program_id}:{slice_.sequence}"
                )
            except (PaperBrokerDatabaseLockedError, ValueError) as error:
                # An exception from the submit call is not proof that no commit occurred.
                # Keep the durable claim: reconciliation must inspect the exact receipt.
                return self._recovery_result(program_id, tuple(executed), f"paper_submission_requires_reconciliation:{error}")
            receipt = self._receipt_for_child(program_id, slice_, child)
            if receipt is None or (fill.order_id, fill.filled_quantity, fill.average_price) != (
                receipt.entry.order_id, receipt.entry.quantity, receipt.entry.fill_price
            ):
                raise ValueError("paper_submission_receipt_result_mismatch")
            before = self._receipt_position(receipt, child)
            self.oms.fill(
                oms_row.client_order_id, fill_id=fill.order_id,
                quantity=fill.filled_quantity, price=fill.average_price,
                broker_order_id=fill.order_id, now=receipt.entry.created_at,
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

    def _recovery_result(self, program_id, executed, reason):
        return InstitutionalExecutionResult(InstitutionalStage.RECOVERY_REQUIRED,
            self.programs.get(program_id), executed, reason)

    def _receipt_for_child(self, program_id, slice_, child):
        receipt = self.broker.submission_receipt(f"{program_id}:{slice_.sequence}", child.tenant_id)
        if receipt is None:
            return None
        payload = receipt.evidence
        program = self.programs.get(program_id)
        if program.risk_authority_version == 1 and payload.get(
                "institutional_risk_authority_sha256") != program.runtime_context_sha256:
            raise ValueError("accounting_recovery_risk_authority_mismatch")
        expected_intent = canonical_order_intent(child)
        if (canonical_order_intent(receipt.order) != expected_intent
                or payload.get("institutional_program") != program_id
                or payload.get("institutional_slice") != slice_.sequence
                or payload.get("order_intent_sha256") != hashlib.sha256(expected_intent.encode()).hexdigest()
                or payload.get("instrument_identity") != bound_identity(child)):
            raise ValueError("accounting_recovery_receipt_attribution_mismatch")
        return receipt

    @staticmethod
    def _receipt_position(receipt, child):
        if not receipt.prior_quantity:
            return None
        return BrokerPosition(child.tenant_id, child.symbol, child.market, child.asset_class,
                              receipt.prior_quantity, receipt.prior_average,
                              child.stop_price, child.take_profit_price)

    def _recover_committed_dispatches(self, program_id, request, parent):
        """Adopt only committed paper receipts, including the pre-OMS crash window."""
        unresolved = False
        for slice_ in self.programs.get(program_id).slices:
            if slice_.state not in {SliceState.PENDING, SliceState.DISPATCHING}:
                continue
            child = replace(parent, quantity=slice_.quantity)
            receipt = self._receipt_for_child(program_id, slice_, child)
            expected_client = self.oms.client_order_id(child,
                f"{request.proposal.decision_id}:slice:{slice_.sequence}")
            if receipt is None:
                if slice_.state is SliceState.DISPATCHING:
                    unresolved = True
                else:
                    try:
                        self.oms.get(expected_client)
                    except KeyError:
                        pass
                    else:
                        # An older writer may have reached the OMS without a program claim.
                        unresolved = True
                continue
            if slice_.client_order_id not in {None, expected_client}:
                raise ValueError("accounting_recovery_dispatch_identity_mismatch")
            self.oms.verify(expected_client)
            current = self.oms.get(expected_client)
            if (self.oms.intent_snapshot(expected_client) != canonical_order_intent(child)
                    or current.state not in {OrderState.SUBMITTED, OrderState.SUBMISSION_UNCERTAIN, OrderState.FILLED}
                    or current.broker_order_id not in {None, receipt.entry.order_id}
                    or current.filled_quantity not in {0, receipt.entry.quantity}):
                raise ValueError("accounting_recovery_dispatch_oms_mismatch")
            # FILLED replays must use the exact original fill; OMS idempotency verifies it.
            self.oms.fill(expected_client, fill_id=receipt.entry.order_id,
                          quantity=receipt.entry.quantity, price=receipt.entry.fill_price,
                          broker_order_id=receipt.entry.order_id, now=receipt.entry.created_at)
            self.oms.verify(expected_client)
            self.programs.mark_filled_unaccounted(program_id, slice_.sequence,
                client_order_id=expected_client, broker_order_id=receipt.entry.order_id,
                pre_fill_average_price=None if receipt.prior_average is None else str(receipt.prior_average))
        return unresolved

    def restore_runtime_context(self, program_id: str, *, tenant_id: str):
        """Bind saved data explicitly; execution still requires its normal fresh gates.

        Not an authenticated operator endpoint or an automatic restart policy. No
        provider, broker submit, accounting repair, halt change or risk release runs.
        """
        stored = self.programs.load_context(program_id, tenant_id=tenant_id)
        program = self.programs.get(program_id)
        self.bind_runtime_context(program_id, request=stored.request,
                                  parent_order=order_from_snapshot(program.parent_order_payload))
        # Do not expose the coordinator's mutable nested objects to the caller.
        return self.programs.load_context(program_id, tenant_id=tenant_id)

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
            or request_fingerprint(request) != InstitutionalPaperCoordinator._bound_request_digest(program)
            or program.parent_order_payload is None
            or canonical_order_intent(parent_order) != program.parent_order_payload
        ):
            raise ValueError("execution_program_runtime_context_mismatch")
        original_request = request
        if program.context_version == 1:
            stored = self.programs.load_context(program_id, tenant_id=request.tenant_id)
            if request_fingerprint(stored.request) != request_fingerprint(request):
                raise ValueError("execution_program_runtime_context_mismatch")
            request = stored.request
            parent_order = order_from_snapshot(program.parent_order_payload)
        self._source_requests[program_id] = original_request
        self._requests[program_id] = request
        self._orders[program_id] = parent_order

    def _assert_runtime_intent(self, program, request, parent) -> None:
        original = self._source_requests.get(program.program_id)
        if original is not None and request_fingerprint(original) != request_fingerprint(request):
            raise ValueError("execution_program_runtime_context_mismatch")
        if (program.parent_order_payload is None
                or canonical_order_intent(parent) != program.parent_order_payload
                or request_fingerprint(request) != InstitutionalPaperCoordinator._bound_request_digest(program)):
            raise ValueError("execution_program_runtime_context_mismatch")

    def reconcile_protective_accounting(self, *, currency: str) -> ProtectiveAccountingReport:
        """Mirror committed exits outside the independent protection/execution path."""
        try:
            return ProtectiveExitAccounting(self.broker, self.accounting, currency=currency).reconcile()
        except (TypeError, ValueError) as error:
            return ProtectiveAccountingReport("unavailable", (), str(error))

    def reconcile_accounting(self, program_id: str) -> InstitutionalExecutionResult:
        request = self._requests.get(program_id)
        parent = self._orders.get(program_id)
        if request is None or parent is None:
            raise ValueError("execution_program_runtime_context_unavailable_after_restart")
        self._assert_runtime_intent(self.programs.get(program_id), request, parent)
        unresolved = self._recover_committed_dispatches(program_id, request, parent)
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
            if (ledger.symbol, ledger.market, ledger.asset_class, ledger.side, ledger.quantity) != (
                child.symbol, child.market, child.asset_class, child.side, child.quantity
            ):
                raise ValueError("accounting_recovery_fill_intent_mismatch")
            if ledger.instrument_identity != bound_identity(child):
                raise ValueError("accounting_recovery_contract_identity_mismatch")
            expected_client = self.oms.client_order_id(
                child, f"{request.proposal.decision_id}:slice:{slice_.sequence}"
            )
            self.oms.verify(slice_.client_order_id)
            oms_order = self.oms.get(slice_.client_order_id)
            if (slice_.client_order_id != expected_client
                    or self.oms.intent_snapshot(slice_.client_order_id) != canonical_order_intent(child)
                    or oms_order.broker_order_id != ledger.order_id
                    or oms_order.filled_quantity != ledger.quantity
                    or oms_order.average_fill_price != ledger.fill_price):
                raise ValueError("accounting_recovery_oms_fill_mismatch")
            receipt = self._receipt_for_child(program_id, slice_, child)
            if receipt is not None and (receipt.entry.order_id != ledger.order_id
                    or slice_.pre_fill_average_price != (None if receipt.prior_average is None else str(receipt.prior_average))):
                raise ValueError("accounting_recovery_cost_basis_receipt_mismatch")
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
        accounting_status = self.reconcile_protective_accounting(currency=request.currency)
        if accounting_status.status not in {"matched", "not_required"}:
            return self._recovery_result(program_id, tuple(finalized), accounting_status.reason)
        program = self.programs.get(program_id)
        if unresolved or self.programs.recovery_required(request.tenant_id):
            return self._recovery_result(program_id, tuple(finalized), "submission_outcome_unresolved")
        if program.state in {ProgramState.FAILED, ProgramState.CANCELLED}:
            return InstitutionalExecutionResult(InstitutionalStage.FAILED, program, tuple(finalized), "program_terminal")
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
