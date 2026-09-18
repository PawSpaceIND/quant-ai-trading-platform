"""Actual daemon-cycle acceptance with synthetic ticks/sources; no SDK transport."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from test_institutional_paper_coordinator import factor_positions, make_request

from quant_ai.accounting.journal import TradingJournal
from quant_ai.accounting.trading import TradingAccounting
from quant_ai.agents.institutional_runtime import InstitutionalRuntimeInputs
from quant_ai.daemon import build_ghost_runner
from quant_ai.decision.edge import EdgePolicy
from quant_ai.domain.models import AssetClass, Instrument, Market
from quant_ai.execution.planner import VolumeBucket
from quant_ai.execution.program import ExecutionProgramJournal
from quant_ai.execution.shared_risk import SharedRiskPolicy
from quant_ai.governance.directives import FounderDirectives
from quant_ai.marketdata.ticker_stream import LiveTick

D = Decimal
NOW = datetime(2026, 9, 17, 7, tzinfo=timezone.utc)
INSTRUMENT = Instrument("TCS", Market.INDIA, AssetClass.EQUITY, "INR", "NSE")


class CycleHarness:
    def __init__(self, root, *, required_book=False):
        self.root = root
        self.now = NOW
        self.programs = ExecutionProgramJournal(root / "programs.sqlite")
        self.journal = TradingJournal(root / "accounting.sqlite", base_currency="INR")
        self.accounting = TradingAccounting(self.journal, "tenant")
        self.accounting.seed_capital("initial", currency="INR", amount=D(100000), base_amount=D(100000), at=NOW)
        self.input_calls = []
        self.missing_inputs = False
        inputs = InstitutionalRuntimeInputs(self.programs, self.accounting,
            self.request, self.factors, self.strategy_exposure, lambda *_: 100000,
            SharedRiskPolicy("synthetic-cycle-account", "INR", D(".005"), "synthetic-cycle-v1"),
            "synthetic-cycle-inputs-v1", EdgePolicy())
        self.history = None
        extra = {}
        if required_book:
            from test_pilot_required_risk_gates import daily
            self.history = daily()
            extra = {"book_risk_history": self.history, "require_book_risk_gates": True}
        self.runner = build_ghost_runner(**extra,zerodha_api_key="synthetic-key", zerodha_access_token="synthetic-token",
            zerodha_instrument_tokens=(1,), zerodha_symbol_by_token={1:"TCS"},
            ib_client=SimpleNamespace(), ib_contracts=(), include_ibkr=False,
            database=root / "paper.sqlite", oms_database=root / "oms.sqlite", tenant_id="tenant",
            log_path=root / "events.jsonl", xai_directory=root / "proofs", halt_file=root / "HALT",
            directives=FounderDirectives(watchlist=(INSTRUMENT,), sector_map={"TCS":"IT_SERVICES"}), pilot_mode=True,
            order_identity_mode="bound_v1", institutional_inputs=inputs)
        self.daemon = self.runner.daemon
        self.daemon.clock = lambda: self.now
        self.runtime = self.daemon.scheduler.pipeline.runtime
        self.broker = self.runtime.broker
        self.oms = self.runtime.oms
        if self.history is not None:
            self.history.fetch(INSTRUMENT, NOW)

    def request(self, packet):
        self.input_calls.append(packet)
        if self.missing_inputs:
            return None
        return replace(make_request(self.broker, p=packet.proposal),
            capital_plan=packet.capital_plan, portfolio=packet.portfolio,
            observed_at=packet.analysis.observed_at, tenant_id=packet.tenant_id,
            projected_factor_positions=self.factors(packet.proposal, packet.portfolio),
            volume_buckets=(VolumeBucket(packet.analysis.observed_at, 100000),))

    def factors(self, order, snapshot):
        observed = dict(snapshot.symbol_quantity)
        values = dict(snapshot.symbol_exposure)
        observed[order.symbol] = observed.get(order.symbol, 0) + order.quantity
        values[order.symbol] = values.get(order.symbol, D(0)) + order.quantity * order.reference_price
        return tuple(replace(factor_positions(value=str(values[symbol]), quantity=quantity)[0], symbol=symbol)
                     for symbol, quantity in observed.items() if quantity)

    def strategy_exposure(self, _):
        return self.daemon.tracker.get_snapshot(self.now).gross_exposure

    def seed(self):
        buffer = self.daemon.tracker.market_feed.buffer
        for index in range(90):
            at = NOW - timedelta(minutes=90-index)
            price = D(100) + D(index // 2) * D(".02") + (D(".04") if index % 2 else D(0))
            buffer.put(LiveTick("TCS", price, D(100000), price-D(".005"), price+D(".005"), at, "synthetic"))
        self.tick(D("100.90"))

    def tick(self, price):
        self.daemon.tracker.market_feed.buffer.put(LiveTick("TCS",price,D(100000),price-D(".005"),price+D(".005"),self.now,"synthetic"))

    def close(self):
        self.oms.close(); self.broker.close(); self.programs.close(); self.journal.close()


@pytest.mark.parametrize("required_book", [False, True])
def test_actual_daemon_cycle_connects_decision_to_programme_receipt_and_accounting(tmp_path, monkeypatch, required_book):
    monkeypatch.setenv("PRAMANA_RELEASE_REVISION", "a" * 40)
    h = CycleHarness(tmp_path, required_book=required_book)
    try:
        h.seed()
        if required_book:
            h.daemon.protection_tick(NOW)
            payload = json.loads(h.broker._connection.execute(
                "SELECT payload FROM pilot_runtime WHERE tenant_id='tenant'").fetchone()[0])
            names = {"sector_concentration", "correlation_adjusted_gross", "book_expected_shortfall"}
            gates = {g["id"]:g for g in payload["riskGates"]["gates"] if g["id"] in names}
            assert set(gates) == names
            assert all(g["armed"] is True and g["required"] is True for g in gates.values())
            assert all(gates[name]["dataReady"] for name in names - {"sector_concentration"})
        brief = asyncio.run(h.daemon.run_once(NOW))
        result = h.daemon.scheduler.last_result
        assert result is not None
        assert result.execution.fill is not None, result.execution.risk_decision.reason
        assert brief.paper_order_ids == (result.execution.fill.order_id,)
        assert len(h.broker.ledger_entries("tenant")) == 1
        assert h.programs.db.execute("SELECT count(*) FROM execution_programs").fetchone()[0] == 1
        assert h.journal.native_balance("tenant", "CASH_AVAILABLE", "INR") == h.broker.get_margin("tenant").cash_balance
        receipt = json.loads(h.broker._connection.execute("SELECT payload FROM paper_decision_evidence").fetchone()[0])
        assert receipt["institutional_program"]
        assert len(receipt["input_matrix"]) == len(result.evidence) > 1
        assert h.programs.get(receipt["institutional_program"]).state.value == "COMPLETE"
        assert h.broker.reconcile("tenant")["status"] == "matched"
        assert h.daemon.strategy_manifest.check(NOW)["status"] == "matched"
    finally:
        h.close()


@pytest.fixture(autouse=True)
def paper_only_environment(monkeypatch):
    monkeypatch.setenv("TRADING_LIVE_MONEY_ACTIVE", "false")
    monkeypatch.setenv("PRAMANA_RELEASE_REVISION", "a" * 40)


def execute_cycle(h):
    asyncio.run(h.daemon.run_once(h.now))
    return h.daemon.scheduler.last_result.execution


def test_actual_cycle_does_not_fall_back_when_institutional_inputs_are_missing(tmp_path, monkeypatch):
    from quant_ai.agents.swarm_runtime import SwarmPaperTradingService
    h = CycleHarness(tmp_path)
    try:
        h.seed(); h.missing_inputs = True
        def forbidden(*args, **kwargs):
            pytest.fail("Selected institutional runtime must never use the direct route")
        monkeypatch.setattr(SwarmPaperTradingService, "_dispatch_approved", forbidden)
        result = execute_cycle(h)
        assert result.fill is None and result.risk_decision.reason == "institutional_request_evidence_missing"
        assert h.input_calls
        assert h.broker.ledger_entries("tenant") == ()
        assert h.programs.db.execute("SELECT count(*) FROM execution_programs").fetchone()[0] == 0
        assert h.oms.all_orders("tenant") == ()
    finally:
        h.close()


def pending_foreign(h):
    from quant_ai.domain.models import InstrumentBoundOrderIntent, Side
    order = InstrumentBoundOrderIntent(symbol="TCS", market=Market.INDIA, side=Side.BUY,
        quantity=1, reference_price=D("100.90"), strategy_id="synthetic-other", asset_class=AssetClass.EQUITY,
        tenant_id="tenant", stop_price=D(99), take_profit_price=D(110), instrument=INSTRUMENT)
    row = h.oms.create(order, decision_id="synthetic-other-decision", now=NOW)
    h.oms.approve_risk(row.client_order_id, now=NOW)
    h.oms.submitted(row.client_order_id, now=NOW)
    return row, order


def test_existing_unresolved_order_still_blocks_initial_daemon_admission(tmp_path):
    h = CycleHarness(tmp_path)
    try:
        h.seed(); row, _ = pending_foreign(h)
        result = execute_cycle(h)
        assert result.fill is None and result.risk_decision.reason == "runtime_identity_oms_recovery_required"
        assert h.input_calls == []
        assert h.oms.get(row.client_order_id).state.value == "SUBMITTED"
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        h.close()


@pytest.mark.parametrize("fault,reason", [
    ("another_order", "runtime_identity_oms_recovery_required"),
    ("uncertain", "runtime_identity_oms_recovery_required"),
    ("quantity", "runtime_identity_oms_or_configuration_unavailable"),
    ("stale_quote", "pilot_stale_entry_price"),
    ("entry_halt", "pilot_halted"),
    ("changed_runtime_policy", "pilot_strategy_manifest_unverified"),
    ("program_claim", "institutional_runtime_inflight_identity_mismatch"),
    ("changed_preflight", "institutional_runtime_final_guard_unavailable"),
])
def test_last_daemon_guard_still_refuses_faults_after_oms_submission(tmp_path, monkeypatch, fault, reason):
    h = CycleHarness(tmp_path)
    try:
        h.seed()
        submitted = h.oms.submitted
        def interrupted(client_id, **kwargs):
            row = submitted(client_id, **kwargs)
            if fault == "another_order":
                monkeypatch.setattr(h.oms, "submitted", submitted)
                pending_foreign(h)
            elif fault == "uncertain":
                h.oms.submission_uncertain(client_id, reason="synthetic unknown", now=NOW)
            elif fault == "quantity":
                h.oms.db.execute("UPDATE oms_orders SET requested_quantity=requested_quantity+1 WHERE client_order_id=?", (client_id,))
                h.oms.db.commit()
            elif fault == "stale_quote":
                h.now += timedelta(minutes=3)
            elif fault == "entry_halt":
                h.daemon.engage_kill_switch("synthetic entry hold")
            elif fault == "program_claim":
                h.programs.db.execute("UPDATE execution_program_slices SET state='PENDING' WHERE client_order_id=?", (client_id,))
                h.programs.db.commit()
            elif fault == "changed_preflight":
                h.runtime.pre_submit_check = lambda _: None
            else:
                h.runtime.max_open_positions += 1
            return row
        monkeypatch.setattr(h.oms, "submitted", interrupted)
        result = execute_cycle(h)
        assert result.fill is None and result.order_state.value == "SUBMISSION_UNCERTAIN"
        assert result.risk_decision.reason.endswith(reason), result.risk_decision.reason
        assert h.broker.ledger_entries("tenant") == ()
        assert h.oms.open_orders("tenant")
        if fault != "program_claim":
            assert h.programs.recovery_required("tenant")
    finally:
        h.close()


@pytest.mark.parametrize("fault", ["wrong_id", "wrong_quantity", "wrong_tenant", "wrong_type", "no_context"])
def test_inflight_context_never_exempts_another_or_modified_intent(tmp_path, fault):
    from quant_ai.governance.runtime_identity import (
        InFlightOmsSubmission,
        runtime_identity_entry_issue,
    )
    h = CycleHarness(tmp_path)
    try:
        h.seed(); row, order = pending_foreign(h)
        context = InFlightOmsSubmission(row.client_order_id, order)
        assert runtime_identity_entry_issue(h.daemon, in_flight=context) is None
        if fault == "wrong_id":
            context = replace(context, client_order_id="other")
        elif fault == "wrong_quantity":
            context = replace(context, order=replace(order, quantity=order.quantity+1))
        elif fault == "wrong_tenant":
            context = replace(context, order=replace(order, tenant_id="other"))
        elif fault == "wrong_type":
            context = {"client_order_id": row.client_order_id, "order": order}
        else:
            context = None
        assert runtime_identity_entry_issue(h.daemon, in_flight=context) == "runtime_identity_oms_recovery_required"
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        h.close()


def test_restart_never_treats_an_existing_submission_as_this_call_inflight(tmp_path):
    h = CycleHarness(tmp_path)
    h.seed(); row, _ = pending_foreign(h); h.close()
    fresh = CycleHarness(tmp_path)
    try:
        fresh.seed()
        result = execute_cycle(fresh)
        assert result.fill is None and result.risk_decision.reason == "runtime_identity_oms_recovery_required"
        assert fresh.input_calls == []
        assert fresh.oms.get(row.client_order_id).state.value == "SUBMITTED"
        assert fresh.broker.ledger_entries("tenant") == ()
    finally:
        fresh.close()


def test_completed_cycle_is_not_submitted_twice_on_the_next_cadence(tmp_path):
    h = CycleHarness(tmp_path)
    try:
        h.seed()
        first = execute_cycle(h)
        assert first.fill is not None
        h.now += timedelta(minutes=10); h.tick(D("100.90"))
        second = execute_cycle(h)
        assert second.fill is None
        assert len(h.broker.ledger_entries("tenant")) == 1
        assert h.programs.db.execute("SELECT count(*) FROM execution_programs").fetchone()[0] == 1
    finally:
        h.close()


def test_daemon_accounting_failure_keeps_original_fill_and_explicit_recovery(tmp_path, monkeypatch):
    h = CycleHarness(tmp_path)
    try:
        h.seed()
        original = h.accounting.buy_security
        def fail(*args, **kwargs):
            raise ValueError("synthetic journal unavailable")
        monkeypatch.setattr(h.accounting, "buy_security", fail)
        result = execute_cycle(h)
        assert result.fill is None and result.order_state.value == "SUBMISSION_UNCERTAIN"
        assert len(h.broker.ledger_entries("tenant")) == 1
        pid = h.programs.db.execute("SELECT program_id FROM execution_programs").fetchone()[0]
        assert h.programs.get(pid).slices[0].state.value == "FILLED_UNACCOUNTED"
        monkeypatch.setattr(h.accounting, "buy_security", original)
        def no_submit(*args, **kwargs):
            pytest.fail("Explicit historical reconciliation must not submit another trade")
        monkeypatch.setattr(h.broker, "submit_with_evidence", no_submit)
        recovered = h.runtime.reconcile_program(pid, tenant_id="tenant")
        assert recovered.program_state == "COMPLETE" and recovered.execution_authorized is False
        assert h.journal.native_balance("tenant", "CASH_AVAILABLE", "INR") == h.broker.get_margin("tenant").cash_balance
        assert len(h.broker.ledger_entries("tenant")) == 1
    finally:
        h.close()


def test_actual_protection_tick_closes_during_halt_then_accounting_reconciles(tmp_path):
    h = CycleHarness(tmp_path)
    try:
        h.seed(); result = execute_cycle(h)
        assert result.fill is not None
        pid = h.programs.db.execute("SELECT program_id FROM execution_programs").fetchone()[0]
        h.daemon.engage_kill_switch("synthetic retained halt")
        h.now += timedelta(seconds=1); h.tick(D(95))
        h.daemon.protection_tick(h.now)
        assert h.broker.get_positions("tenant") == ()
        assert len(h.broker.ledger_entries("tenant")) == 2
        assert h.runtime.reconcile_program(pid, tenant_id="tenant").program_state == "COMPLETE"
        assert h.journal.native_balance("tenant", "SECURITIES_COST", "INR") == D(0)
        assert h.journal.native_balance("tenant", "CASH_AVAILABLE", "INR") == h.broker.get_margin("tenant").cash_balance
        assert h.daemon.kill_switch.engaged
        assert h.daemon.kill_switch.reason == "synthetic retained halt"
    finally:
        h.close()


def test_off_hours_cycle_does_not_admit_an_institutional_programme(tmp_path):
    h = CycleHarness(tmp_path)
    try:
        h.now = NOW.replace(hour=1)
        brief = asyncio.run(h.daemon.run_once(h.now))
        assert brief.market_state.value != "REGULAR_HOURS"
        assert h.input_calls == [] and h.broker.ledger_entries("tenant") == ()
        assert h.programs.db.execute("SELECT count(*) FROM execution_programs").fetchone()[0] == 0
    finally:
        h.close()



def test_already_committed_fill_cannot_use_the_inflight_exception(tmp_path, monkeypatch):
    from quant_ai.governance.runtime_identity import (
        InFlightOmsSubmission,
        runtime_identity_entry_issue,
    )
    from quant_ai.orders.intent import order_from_snapshot
    h = CycleHarness(tmp_path)
    try:
        h.seed()
        def fail_oms(*args, **kwargs):
            raise ValueError("synthetic interruption before OMS fill commit")
        monkeypatch.setattr(h.oms, "fill", fail_oms)
        result = execute_cycle(h)
        assert result.fill is None and result.order_state.value == "SUBMISSION_UNCERTAIN"
        assert len(h.broker.ledger_entries("tenant")) == 1
        pid = h.programs.db.execute("SELECT program_id FROM execution_programs").fetchone()[0]
        program = h.programs.get(pid)
        intent = order_from_snapshot(program.parent_order_payload)
        witness = InFlightOmsSubmission(program.slices[0].client_order_id, intent)
        assert runtime_identity_entry_issue(h.daemon, in_flight=witness) == "runtime_identity_oms_recovery_required"
        assert h.oms.get(witness.client_order_id).state.value == "SUBMITTED"
    finally:
        h.close()


def test_inflight_hook_binding_does_not_silently_replace_initial_admission(tmp_path):
    h = CycleHarness(tmp_path)
    try:
        selected = h.runtime.pre_submit_check
        h.runtime.bind_inflight_preflight(h.daemon._pilot_inflight_pre_submit)
        assert h.runtime.pre_submit_check == selected
        with pytest.raises(ValueError, match="institutional_runtime_inflight_preflight_invalid"):
            h.runtime.bind_inflight_preflight(lambda *_: None)
        with pytest.raises(ValueError, match="institutional_runtime_inflight_preflight_invalid"):
            h.runtime.bind_inflight_preflight(None)
        h.seed()
        assert execute_cycle(h).fill is not None
    finally:
        h.close()
