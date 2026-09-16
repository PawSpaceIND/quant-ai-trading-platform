from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant_ai.accounting.journal import TradingJournal
from quant_ai.accounting.trading import TradingAccounting
from quant_ai.agents.swarm import TradeProposal
from quant_ai.decision.edge import EdgeEvidence
from quant_ai.domain.models import AssetClass, Market, PortfolioSnapshot, RiskMode, Side
from quant_ai.execution.institutional import (
    InstitutionalPaperCoordinator,
    InstitutionalStage,
    InstitutionalTradeRequest,
)
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.execution.planner import ExecutionAlgorithm, ExecutionConstraints, VolumeBucket
from quant_ai.execution.program import ExecutionProgramJournal, ProgramState, SliceState
from quant_ai.orders.oms import DurableOms
from quant_ai.planning.capital import CapitalGoalEngine, CapitalPlanRequest
from quant_ai.portfolio.optimizer import PortfolioOptimizationPolicy, StrategyOpportunity
from quant_ai.risk.factor_liquidity import FactorLiquidityPolicy, FactorLiquidityPosition
from quant_ai.risk.warden import RiskWarden

D = Decimal
NOW = datetime(2026, 9, 16, 10, tzinfo=timezone.utc)
H = "a" * 64

def proposal(quantity=10, side=Side.BUY, decision_id="decision-1"):
    return TradeProposal(
        decision_id, "INFY", Market.INDIA, "India", AssetClass.EQUITY, side,
        quantity, D(100), D(95), D(110), D("0.8"), D("0.02"), D("0.03"),
        ("test",),
    )


def edge(probability="0.70"):
    return EdgeEvidence(
        D(probability), D("0.02"), D("0.02"), D("0.05"), D("0.02"),
        D("0.001"), 100, H,
    )


def capital():
    return CapitalGoalEngine().recommend(CapitalPlanRequest(
        D(100000), D("0.8"), D("0.20"), expected_edge=D("0.02"),
        requested_mode=RiskMode.BALANCED,
    ))


def opportunity():
    return StrategyOpportunity(
        "atlas-strategy", D("0.10"), D("0.10"), D("0.05"), D("0.9"),
        D("0.20"), D("0.20"),
    )

def allocation_policy():
    return PortfolioOptimizationPolicy(
        max_gross_weight=D("0.20"), max_strategy_weight=D("0.20"),
        max_portfolio_volatility=D("0.20"), risk_aversion=D("0.1"),
        turnover_penalty=D(0), weight_step=D("0.01"),
    )


def factor_policy():
    return FactorLiquidityPolicy(
        {"MARKET": D("0.30"), "TECH": D("0.30")},
        max_daily_participation=D("0.10"), max_liquidation_days=D(5),
        max_total_liquidation_cost_fraction=D("0.02"),
    )


def factor_positions(value="1000", quantity=10):
    return (FactorLiquidityPosition(
        "INFY", D(value), quantity, D(100),
        {"MARKET": D("0.5"), "TECH": D("0.5")},
        D(10000), D("0.5"), D("0.01"),
    ),)


def snapshot_from_broker(broker, daily_total_pnl=None):
    daily_total_pnl = D(0) if daily_total_pnl is None else daily_total_pnl
    margin = broker.get_margin("tenant")
    positions = broker.get_positions("tenant")
    exposure = {item.symbol: item.average_price * item.quantity for item in positions}
    quantities = {item.symbol: item.quantity for item in positions}
    gross = sum(exposure.values(), D(0))
    return PortfolioSnapshot(
        equity=margin.cash_balance + gross,
        daily_realized_pnl=D(0), gross_exposure=gross, peak_equity=D(100000),
        symbol_exposure=exposure,
        asset_exposure={AssetClass.EQUITY: gross} if gross else {},
        symbol_quantity=quantities, daily_total_pnl=daily_total_pnl,
        country_exposure={"India": gross} if gross else {},
        available_margin=margin.available_margin,
    )


def make_request(broker, *, p=None, algorithm=ExecutionAlgorithm.IMMEDIATE, buckets=None):
    p = p or proposal()
    return InstitutionalTradeRequest(
        proposal=p, strategy_id="atlas-strategy", edge_evidence=edge(),
        capital_plan=capital(), portfolio=snapshot_from_broker(broker),
        strategy_opportunities=(opportunity(),), strategy_correlations={},
        current_strategy_weights={}, allocation_policy=allocation_policy(),
        projected_factor_positions=factor_positions(
            str(p.reference_price * p.quantity), p.quantity
        ),
        factor_policy=factor_policy(), execution_algorithm=algorithm,
        execution_constraints=ExecutionConstraints(max_participation=D("0.10")),
        volume_buckets=buckets or (VolumeBucket(NOW, 1000),),
        tenant_id="tenant", currency="INR", base_rate=D(1), observed_at=NOW,
    )


class Harness:
    def __init__(self, tmp_path, accounting_cls=TradingAccounting):
        self.broker = PaperBrokerService(
            tmp_path / "paper.sqlite", starting_capital=D(100000), slippage_bps=D(0)
        )
        self.oms = DurableOms(tmp_path / "oms.sqlite")
        self.programs = ExecutionProgramJournal(tmp_path / "programs.sqlite")
        self.journal = TradingJournal(tmp_path / "accounting.sqlite", base_currency="INR")
        self.accounting = accounting_cls(self.journal, "tenant")
        self.accounting.seed_capital(
            "capital", currency="INR", amount=D(100000), base_amount=D(100000), at=NOW
        )
        self.daily_total_pnl = D(0)
        self.slice_volume = 1000
        self.coordinator = InstitutionalPaperCoordinator(
            broker=self.broker, oms=self.oms, programs=self.programs,
            accounting=self.accounting, warden=RiskWarden(),
            snapshot_provider=lambda: snapshot_from_broker(
                self.broker, self.daily_total_pnl
            ),
            factor_position_provider=self.factor_provider,
            strategy_exposure_provider=lambda _strategy: self.strategy_exposure(),
            slice_volume_provider=lambda _program, _sequence, _now: self.slice_volume,
        )

    def strategy_exposure(self):
        return sum(
            (
                item.notional for item in self.broker.ledger_entries("tenant")
                if item.side is Side.BUY
            ),
            D(0),
        )

    def factor_provider(self, order, snapshot):
        qty = snapshot.symbol_quantity.get(order.symbol, 0)
        value = snapshot.symbol_exposure.get(order.symbol, D(0))
        sign = 1 if order.side is Side.BUY else -1
        qty = max(0, qty + sign * order.quantity)
        value = max(D(0), value + sign * order.reference_price * order.quantity)
        return factor_positions(str(value), qty) if qty else ()

    def close(self):
        self.journal.close()
        self.programs.close()
        self.oms.close()
        self.broker.close()


def test_immediate_trade_closes_edge_to_double_entry_end_to_end(tmp_path):
    h = Harness(tmp_path)
    try:
        request = make_request(h.broker)
        prep = h.coordinator.prepare(request)
        assert prep.approved and prep.approved_order is not None
        result = h.coordinator.execute_due(prep.program.program_id, now=NOW)
        assert result.stage is InstitutionalStage.COMPLETE
        assert result.program.state is ProgramState.COMPLETE
        assert result.program.executed_quantity == 10
        assert len(h.broker.ledger_entries("tenant")) == 1
        client_id = result.program.slices[0].client_order_id
        assert client_id is not None and h.oms.get(client_id).state.value == "FILLED"
        assert h.journal.native_balance(
            "tenant", "CASH_AVAILABLE", "INR"
        ) == h.broker.get_margin("tenant").cash_balance
        assert h.journal.native_balance(
            "tenant", "SECURITIES_COST", "INR"
        ) == D(1000)
        assert h.journal.verify("tenant")["verified"] is True
    finally:
        h.close()


def test_twap_executes_only_due_slice_and_rechecks_risk_before_next(tmp_path):
    h = Harness(tmp_path)
    try:
        p = proposal(quantity=20)
        request = make_request(
            h.broker, p=p, algorithm=ExecutionAlgorithm.TWAP,
            buckets=(
                VolumeBucket(NOW, 1000),
                VolumeBucket(NOW + timedelta(minutes=10), 1000),
            ),
        )
        prep = h.coordinator.prepare(request)
        assert prep.approved
        first = h.coordinator.execute_due(prep.program.program_id, now=NOW)
        assert first.executed_sequences == (1,)
        assert first.program.state is ProgramState.ACTIVE
        assert len(h.broker.ledger_entries("tenant")) == 1
        h.daily_total_pnl = D("-2000")
        second = h.coordinator.execute_due(
            prep.program.program_id, now=NOW + timedelta(minutes=10)
        )
        assert second.stage is InstitutionalStage.FAILED
        assert second.program.state is ProgramState.FAILED
        assert len(h.broker.ledger_entries("tenant")) == 1
    finally:
        h.close()


def test_due_slice_refuses_when_live_liquidity_falls_below_planned_capacity(tmp_path):
    h = Harness(tmp_path)
    try:
        request = make_request(h.broker)
        prep = h.coordinator.prepare(request)
        assert prep.approved
        h.slice_volume = 50  # 10% participation -> only 5 shares of live capacity.
        result = h.coordinator.execute_due(prep.program.program_id, now=NOW)
        assert result.stage is InstitutionalStage.FAILED
        assert result.reason.startswith("execution_liquidity_recheck_failed")
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        h.close()


class FailOnceAccounting(TradingAccounting):
    def __init__(self, journal, tenant_id):
        super().__init__(journal, tenant_id)
        self.failed = False

    def buy_security(self, *args, **kwargs):
        if not self.failed:
            self.failed = True
            raise ValueError("synthetic_accounting_crash")
        return super().buy_security(*args, **kwargs)


def test_committed_fill_recovers_accounting_after_restart_without_resubmission(tmp_path):
    h = Harness(tmp_path, accounting_cls=FailOnceAccounting)
    try:
        request = make_request(h.broker)
        prep = h.coordinator.prepare(request)
        assert prep.approved and prep.approved_order is not None
        first = h.coordinator.execute_due(prep.program.program_id, now=NOW)
        assert first.stage is InstitutionalStage.FAILED
        assert first.program.slices[0].state is SliceState.FILLED_UNACCOUNTED
        assert len(h.broker.ledger_entries("tenant")) == 1
        assert h.journal.native_balance("tenant", "SECURITIES_COST", "INR") == 0

        h.programs.close(); h.oms.close(); h.journal.close()
        programs = ExecutionProgramJournal(tmp_path / "programs.sqlite")
        oms = DurableOms(tmp_path / "oms.sqlite")
        journal = TradingJournal(tmp_path / "accounting.sqlite", base_currency="INR")
        recovered = InstitutionalPaperCoordinator(
            broker=h.broker, oms=oms, programs=programs,
            accounting=TradingAccounting(journal, "tenant"), warden=RiskWarden(),
            snapshot_provider=lambda: snapshot_from_broker(h.broker),
            factor_position_provider=h.factor_provider,
            strategy_exposure_provider=lambda _strategy: h.strategy_exposure(),
            slice_volume_provider=lambda _program, _sequence, _now: h.slice_volume,
        )
        recovered.bind_runtime_context(
            prep.program.program_id, request=request, parent_order=prep.approved_order
        )
        final = recovered.reconcile_accounting(prep.program.program_id)
        assert final.stage is InstitutionalStage.COMPLETE
        assert len(h.broker.ledger_entries("tenant")) == 1
        assert journal.native_balance("tenant", "SECURITIES_COST", "INR") == D(1000)
        assert journal.native_balance(
            "tenant", "CASH_AVAILABLE", "INR"
        ) == h.broker.get_margin("tenant").cash_balance
        journal.close(); oms.close(); programs.close()
        h.journal = TradingJournal(":memory:", base_currency="INR")
        h.oms = DurableOms(":memory:")
        h.programs = ExecutionProgramJournal(":memory:")
    finally:
        h.close()


def test_restart_rebind_rejects_changed_governance_context(tmp_path):
    h = Harness(tmp_path)
    try:
        p = proposal(quantity=20)
        request = make_request(
            h.broker, p=p, algorithm=ExecutionAlgorithm.TWAP,
            buckets=(
                VolumeBucket(NOW, 1000),
                VolumeBucket(NOW + timedelta(minutes=10), 1000),
            ),
        )
        prep = h.coordinator.prepare(request)
        assert prep.approved and prep.approved_order is not None
        changed = InstitutionalTradeRequest(**{**request.__dict__, "base_rate": D("1.01")})
        with pytest.raises(ValueError, match="runtime_context_mismatch"):
            h.coordinator.bind_runtime_context(
                prep.program.program_id, request=changed, parent_order=prep.approved_order
            )
    finally:
        h.close()


def test_due_slice_stops_when_strategy_allocation_headroom_disappears(tmp_path):
    h = Harness(tmp_path)
    try:
        p = proposal(quantity=20, decision_id="allocation-drift")
        request = make_request(
            h.broker,
            p=p,
            algorithm=ExecutionAlgorithm.TWAP,
            buckets=(
                VolumeBucket(NOW, 1000),
                VolumeBucket(NOW + timedelta(minutes=10), 1000),
            ),
        )
        prep = h.coordinator.prepare(request)
        assert prep.approved and prep.program is not None
        first = h.coordinator.execute_due(prep.program.program_id, now=NOW)
        assert first.executed_sequences == (1,)
        h.coordinator.strategy_exposure_provider = lambda _strategy: D("19950")
        second = h.coordinator.execute_due(
            prep.program.program_id, now=NOW + timedelta(minutes=10)
        )
        assert second.stage is InstitutionalStage.FAILED
        assert second.reason == "strategy_allocation_notional_limit_at_slice"
        assert len(h.broker.ledger_entries("tenant")) == 1
    finally:
        h.close()


def test_due_slice_stops_when_factor_concentration_deteriorates(tmp_path):
    h = Harness(tmp_path)
    try:
        request = make_request(h.broker, p=proposal(decision_id="factor-drift"))
        prep = h.coordinator.prepare(request)
        assert prep.approved and prep.program is not None
        h.coordinator.factor_position_provider = (
            lambda _order, _snapshot: factor_positions(value="70000", quantity=700)
        )
        result = h.coordinator.execute_due(prep.program.program_id, now=NOW)
        assert result.stage is InstitutionalStage.FAILED
        assert result.reason.startswith("factor_exposure_limit:")
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        h.close()


def test_missing_edge_evidence_cannot_create_execution_program(tmp_path):
    h = Harness(tmp_path)
    try:
        request = make_request(h.broker, p=proposal(decision_id="no-edge"))
        request = InstitutionalTradeRequest(**{**request.__dict__, "edge_evidence": None})
        prep = h.coordinator.prepare(request)
        assert not prep.approved
        assert prep.stage is InstitutionalStage.EDGE
        assert prep.reason == "edge_evidence_required"
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        h.close()


def test_covered_exit_bypasses_edge_allocation_and_reconciles_accounting(tmp_path):
    h = Harness(tmp_path)
    try:
        entry_request = make_request(
            h.broker, p=proposal(decision_id="roundtrip-entry")
        )
        entry = h.coordinator.prepare(entry_request)
        assert entry.approved and entry.program is not None
        assert h.coordinator.execute_due(
            entry.program.program_id, now=NOW
        ).stage is InstitutionalStage.COMPLETE

        exit_proposal = proposal(
            quantity=10, side=Side.SELL, decision_id="roundtrip-exit"
        )
        exit_request = make_request(h.broker, p=exit_proposal)
        exit_request = InstitutionalTradeRequest(
            **{**exit_request.__dict__, "edge_evidence": None,
               "strategy_opportunities": (), "strategy_correlations": {}}
        )
        exit_prep = h.coordinator.prepare(exit_request)
        assert exit_prep.approved and exit_prep.program is not None
        assert exit_prep.edge is None and exit_prep.allocation is None
        closed = h.coordinator.execute_due(exit_prep.program.program_id, now=NOW)
        assert closed.stage is InstitutionalStage.COMPLETE
        assert h.broker.get_positions("tenant") == ()
        assert h.journal.native_balance("tenant", "SECURITIES_COST", "INR") == D(0)
        assert h.journal.native_balance(
            "tenant", "CASH_AVAILABLE", "INR"
        ) == h.broker.get_margin("tenant").cash_balance
        assert h.journal.verify("tenant")["verified"] is True
    finally:
        h.close()
