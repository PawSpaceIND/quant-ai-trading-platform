"""Opt-in immediate NSE paper bridge from the real swarm path to the coordinator.

Required calibrated edge, allocation, factor and liquidity inputs come from the
explicit request provider; model confidence is never substituted for them. There
is no direct-submit fallback. This is not an automatic recovery or live transport.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, replace
from threading import RLock

from quant_ai.accounting.trading import TradingAccounting
from quant_ai.agents.swarm import AgentAnalysisRequest, InstrumentBoundTradeProposal, TradeProposal
from quant_ai.agents.swarm_runtime import SwarmExecutionResult, SwarmPaperTradingService
from quant_ai.brokers.base import ExecutionResult
from quant_ai.decision.edge import CalibratedEdgeGate, EdgePolicy
from quant_ai.domain.models import AssetClass, Market, PortfolioSnapshot, Side
from quant_ai.execution.institutional import (
    FactorPositionProvider,
    InstitutionalPaperCoordinator,
    InstitutionalStage,
    InstitutionalTradeRequest,
    SliceVolumeProvider,
    StrategyExposureProvider,
)
from quant_ai.execution.planner import ExecutionAlgorithm
from quant_ai.execution.program import ExecutionProgramJournal
from quant_ai.execution.shared_risk import SharedRiskPolicy
from quant_ai.governance.runtime_manifest import stable
from quant_ai.orders.intent import canonical_order_intent
from quant_ai.orders.oms import DurableOms
from quant_ai.orders.state import OrderState
from quant_ai.planning.capital import CapitalPlan
from quant_ai.risk.warden import WardenDecision

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class InstitutionalDecisionInput:
    analysis: AgentAnalysisRequest
    proposal: TradeProposal
    capital_plan: CapitalPlan
    portfolio: PortfolioSnapshot
    tenant_id: str


@dataclass(frozen=True)
class InstitutionalRuntimeInputs:
    """Operator-selected dependencies, not credentials or fabricated data defaults."""
    programs: ExecutionProgramJournal
    accounting: TradingAccounting
    request_provider: Callable[[InstitutionalDecisionInput], InstitutionalTradeRequest | None]
    factor_position_provider: FactorPositionProvider
    strategy_exposure_provider: StrategyExposureProvider
    slice_volume_provider: SliceVolumeProvider
    shared_risk_policy: SharedRiskPolicy
    source_revision: str
    edge_policy: EdgePolicy

    def __post_init__(self):
        if (not isinstance(self.programs, ExecutionProgramJournal)
                or not isinstance(self.accounting, TradingAccounting)
                or type(self.shared_risk_policy) is not SharedRiskPolicy
                or type(self.edge_policy) is not EdgePolicy
                or self.shared_risk_policy.currency != "INR"
                or self.accounting.journal.base_currency != "INR"
                or type(self.source_revision) is not str
                or not self.source_revision.strip() or self.source_revision != self.source_revision.strip()
                or len(self.source_revision) > 180
                or any(not callable(fn) for fn in (self.request_provider,
                    self.factor_position_provider, self.strategy_exposure_provider,
                    self.slice_volume_provider))):
            raise ValueError("institutional_runtime_inputs_invalid")
        if str(self.programs.path) == ":memory:" or str(self.accounting.journal.path) == ":memory:":
            raise ValueError("institutional_runtime_durable_stores_required")


class InstitutionalSwarmPaperTradingService(SwarmPaperTradingService):
    """The ordinary sync/async swarm entry points, with institutional dispatch."""

    def __init__(self, *, institutional_inputs: InstitutionalRuntimeInputs, **kwargs):
        if os.getenv("TRADING_LIVE_MONEY_ACTIVE", "false").strip().lower() == "true":
            raise ValueError("institutional_runtime_paper_only")
        if type(institutional_inputs) is not InstitutionalRuntimeInputs:
            raise ValueError("institutional_runtime_inputs_required")
        super().__init__(**kwargs)
        if not isinstance(self.oms, DurableOms):
            raise TypeError("institutional_runtime_durable_oms_required")
        self._inputs = institutional_inputs
        self._route_lock = RLock()
        self._coordinator = InstitutionalPaperCoordinator(
            broker=self.broker, oms=self.oms, programs=institutional_inputs.programs,
            accounting=institutional_inputs.accounting, warden=self.warden,
            snapshot_provider=self._snapshot,
            factor_position_provider=institutional_inputs.factor_position_provider,
            strategy_exposure_provider=institutional_inputs.strategy_exposure_provider,
            slice_volume_provider=institutional_inputs.slice_volume_provider,
            shared_risk_policy=institutional_inputs.shared_risk_policy,
            edge_gate=CalibratedEdgeGate(institutional_inputs.edge_policy),
        )

    @property
    def institutional_configuration(self):
        def identity(fn):
            return f"{getattr(fn, '__module__', '')}.{getattr(fn, '__qualname__', type(fn).__qualname__)}"
        def path_id(path):
            return hashlib.sha256(str(path.resolve()).encode()).hexdigest()
        return {
            "schema": "pramana.institutional_swarm.immediate.v1",
            "sourceRevision": self._inputs.source_revision,
            "tenant": self._inputs.accounting.tenant_id,
            "programsPathSha256": path_id(self._inputs.programs.path),
            "accountingPathSha256": path_id(self._inputs.accounting.journal.path),
            "sharedRisk": stable(self._coordinator.shared_risk_policy),
            "edgePolicy": stable(self._coordinator.edge_gate.policy),
            "providers": {name: identity(getattr(self._inputs, name)) for name in (
                "request_provider", "factor_position_provider", "strategy_exposure_provider",
                "slice_volume_provider")},
            "scheduledExecution": False,
            "automaticRecovery": False,
        }

    def _snapshot(self):
        if not callable(self.snapshot_provider):
            raise TypeError("institutional_runtime_snapshot_unavailable")
        result = self.snapshot_provider()
        if type(result) is not PortfolioSnapshot:
            raise ValueError("institutional_runtime_snapshot_invalid")
        return result

    def _assert_wiring(self):
        if (self._coordinator.broker is not self.broker or self._coordinator.oms is not self.oms
                or self._coordinator.warden is not self.warden
                or self._coordinator.programs is not self._inputs.programs
                or self._coordinator.accounting is not self._inputs.accounting
                or self._coordinator.factor_position_provider is not self._inputs.factor_position_provider
                or self._coordinator.strategy_exposure_provider is not self._inputs.strategy_exposure_provider
                or self._coordinator.slice_volume_provider is not self._inputs.slice_volume_provider):
            raise ValueError("institutional_runtime_wiring_changed")
        if not callable(self.pre_submit_check):
            raise TypeError("institutional_runtime_preflight_unavailable")

    def _execute_proposal(self, request, weighted_evidence, proposal, plan, portfolio,
                          country_exposure, tenant_id):
        # Do not hold a broker lock while acquiring the programme writer lock.
        # The coordinator rechecks its fresh inputs, and the operating guard below
        # runs under the broker lock at the final submission boundary.
        with self._route_lock:
            try:
                self._assert_wiring()
                portfolio = self._snapshot()
            except (ValueError, TypeError, OSError, RuntimeError, sqlite3.Error):
                stress = self.stress_agent.evaluate(proposal, portfolio)
                return self._refuse(request, weighted_evidence, proposal, stress,
                                    "institutional_runtime_unavailable", tenant_id)
            return self._execute_proposal_locked(request, weighted_evidence, proposal, plan,
                                                 portfolio, portfolio.country_exposure, tenant_id)

    def _refuse(self, request, evidence, proposal, stress, reason, tenant_id,
                state=OrderState.REJECTED):
        risk = self.warden.reject(reason, proposal, tenant_id)
        trace = self.xai_logger.log(request, evidence, proposal, stress, risk)
        return SwarmExecutionResult(proposal, risk, None, stress, trace, state)

    def _dispatch_approved(self, request, weighted_evidence, proposal, plan, portfolio,
                           stress, risk, lifecycle, tenant_id):
        def refuse(reason, state=OrderState.REJECTED):
            return self._refuse(request, weighted_evidence, proposal, stress, reason, tenant_id, state)
        instrument = getattr(proposal, "instrument", None)
        if (not isinstance(proposal, InstrumentBoundTradeProposal) or instrument is None
                or instrument.market is not Market.INDIA or instrument.exchange != "NSE"
                or instrument.currency != "INR"
                or instrument.asset_class not in {AssetClass.EQUITY, AssetClass.ETF}):
            return refuse("institutional_runtime_bound_nse_cash_required")
        if tenant_id != self._inputs.accounting.tenant_id:
            return refuse("institutional_accounting_scope_mismatch")
        packet = InstitutionalDecisionInput(request, proposal, plan, portfolio, tenant_id)
        try:
            candidate = self._inputs.request_provider(deepcopy(packet))
        except (ValueError, TypeError, OSError, RuntimeError, ArithmeticError):
            return refuse("institutional_request_provider_unavailable")
        if candidate is None:
            return refuse("institutional_request_evidence_missing")
        if (type(candidate) is not InstitutionalTradeRequest or candidate.proposal != proposal
                or candidate.capital_plan != plan or candidate.portfolio != portfolio
                or candidate.tenant_id != tenant_id or candidate.observed_at != request.observed_at):
            return refuse("institutional_request_identity_mismatch")
        if (candidate.execution_algorithm is not ExecutionAlgorithm.IMMEDIATE
                or len(candidate.volume_buckets) != 1
                or candidate.volume_buckets[0].at != candidate.observed_at):
            return refuse("institutional_runtime_immediate_schedule_required")
        trace = self.xai_logger.build(request, weighted_evidence, proposal, stress, risk)
        trace_data = json.loads(self.xai_logger.to_json(trace))
        # Preserve the exact decision evidence in the immutable request as well as
        # in the broker's atomic fill receipt. No external evidence is fabricated.
        prepared_proposal = replace(proposal, provenance={**(proposal.provenance or {}),
            "institutional_source_trace": deepcopy(trace_data),
            "institutional_input_source": self._inputs.source_revision})
        candidate = replace(candidate, proposal=prepared_proposal)
        try:
            self._assert_wiring()
            prepared = self._coordinator.prepare(candidate)
        except (ValueError, TypeError, OSError, RuntimeError, ArithmeticError, sqlite3.Error):
            return refuse("institutional_preparation_unavailable")
        if not prepared.approved:
            return refuse(prepared.reason)
        if prepared.execution_plan is None or len(prepared.execution_plan.slices) != 1:
            return refuse("institutional_program_requires_review", OrderState.SUBMISSION_UNCERTAIN)
        try:
            result = self._coordinator.execute_due(prepared.program.program_id,
                now=request.observed_at,
                pre_submit_check=lambda child: self._operating_guard(
                    child, request, proposal, plan, tenant_id),
                decision_trace=trace_data)
        except (ValueError, TypeError, OSError, RuntimeError, ArithmeticError, sqlite3.Error):
            return refuse("institutional_execution_recovery_required", OrderState.SUBMISSION_UNCERTAIN)
        if result.stage is not InstitutionalStage.COMPLETE or result.executed_sequences != (1,):
            # This response never means the broker failed to commit. The canonical
            # receipt and durable recovery state are authoritative; do not resubmit.
            return refuse("institutional_recovery_required:" + result.reason,
                          OrderState.SUBMISSION_UNCERTAIN)
        part = result.program.slices[0]
        entry = next((row for row in self.broker.ledger_entries(tenant_id)
                      if row.order_id == part.broker_order_id), None)
        if entry is None:
            return refuse("institutional_receipt_unavailable", OrderState.SUBMISSION_UNCERTAIN)
        fill = ExecutionResult(entry.order_id, "FILLED", entry.quantity, entry.fill_price)
        actual_risk = WardenDecision(True, "institutional_complete", prepared.approved_order)
        trace = replace(trace, order_id=fill.order_id)
        try:
            self.xai_logger.record(trace)
        except OSError:
            LOGGER.exception("institutional_trace_projection_failed; canonical receipt retained")
        return SwarmExecutionResult(proposal, actual_risk, fill, stress, trace, OrderState.FILLED)

    def _operating_guard(self, child, analysis, proposal, plan, tenant_id):
        """Re-run operating controls after input providers and OMS callbacks."""
        try:
            self._assert_wiring()
            if os.getenv("TRADING_LIVE_MONEY_ACTIVE", "false").strip().lower() == "true":
                return "institutional_runtime_paper_only"
            veto = self.pre_submit_check(proposal)
            if veto is not None:
                return veto
            snapshot = self._snapshot()
            held = self._held_quantity(child, tenant_id)
            covered = child.side is Side.SELL and 0 < child.quantity <= held
            if self.kill_switch.engaged and not covered:
                return "kill_switch_engaged:" + str(self.kill_switch.reason)
            if child.side is Side.SELL and not covered:
                return "paper_naked_sell_disabled"
            if child.side is Side.BUY:
                if held and not self.allow_position_scaling:
                    return "position_already_open"
                if (not held and self.max_open_positions is not None
                        and len(self.broker.get_positions(tenant_id)) >= self.max_open_positions):
                    return "max_open_positions_reached"
                if self._in_exit_cooldown(child, tenant_id, analysis.observed_at):
                    return "re_entry_cooldown_active"
                if not self.stress_agent.evaluate(proposal, snapshot).passed:
                    return "STRESS_VETO"
            fresh_risk = self.warden.evaluate(proposal, plan, snapshot,
                country_exposure=snapshot.country_exposure, tenant_id=tenant_id,
                now=analysis.observed_at)
            if not fresh_risk.approved or fresh_risk.order is None:
                return fresh_risk.reason
            fresh_order = replace(fresh_risk.order, strategy_id=child.strategy_id)
            if canonical_order_intent(fresh_order) != canonical_order_intent(child):
                return "institutional_runtime_final_order_mismatch"
        except (ValueError, TypeError, OSError, RuntimeError, ArithmeticError, sqlite3.Error):
            return "institutional_runtime_final_guard_unavailable"
        return None
