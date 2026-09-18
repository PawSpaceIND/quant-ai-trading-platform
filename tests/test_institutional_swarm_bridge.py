"""Disposable paper bridge; all source/calibration/liquidity values are synthetic."""
import json
from dataclasses import replace
from decimal import Decimal

import pytest
from test_institutional_paper_coordinator import (
    NOW,
    Harness,
    make_request,
    proposal,
    snapshot_from_broker,
)

from quant_ai.agents.contracts import AgentDomain, AgentEvidence, Stance
from quant_ai.agents.institutional_runtime import (
    InstitutionalRuntimeInputs,
    InstitutionalSwarmPaperTradingService,
)
from quant_ai.agents.swarm import AgentAnalysisRequest, InstrumentBoundTradeProposal
from quant_ai.agents.swarm_runtime import SwarmPaperTradingService
from quant_ai.decision.edge import EdgePolicy
from quant_ai.domain.models import AssetClass, Instrument, Market, Side
from quant_ai.execution.audit import XAITraceLogger
from quant_ai.execution.shared_risk import SharedRiskPolicy
from quant_ai.governance.runtime_identity import configure_runtime_identity
from quant_ai.orders.state import OrderState

D = Decimal


class BridgeHarness(Harness):
    def __init__(self, root):
        super().__init__(root)
        self.instrument = Instrument("INFY", Market.INDIA, AssetClass.EQUITY, "INR", "NSE")
        configure_runtime_identity(self.broker, (self.instrument,), "tenant", "bound_v1",
                                   self.oms.path, oms=self.oms)
        self.inputs = InstitutionalRuntimeInputs(
            self.programs, self.accounting, self.request_provider, self.factor_provider,
            lambda _: self.strategy_exposure(), lambda *_: 1000,
            SharedRiskPolicy("synthetic-account", "INR", D(".005"), "synthetic-v1"),
            "synthetic-inputs-v1", EdgePolicy(),
        )
        self.runtime = self.new_runtime()

    def request_provider(self, packet):
        return replace(make_request(self.broker, p=packet.proposal),
                       capital_plan=packet.capital_plan, portfolio=packet.portfolio,
                       tenant_id=packet.tenant_id, observed_at=packet.analysis.observed_at,
                       projected_factor_positions=self.factor_provider(packet.proposal, packet.portfolio))

    def new_runtime(self):
        runtime = InstitutionalSwarmPaperTradingService(
            institutional_inputs=self.inputs, broker=self.broker, oms=self.oms,
            warden=self.coordinator.warden, xai_logger=XAITraceLogger(self.journal.path.parent / "proofs"),
            max_open_positions=2,
        )
        runtime.snapshot_provider = lambda: snapshot_from_broker(self.broker)
        runtime.pre_submit_check = lambda _: None
        return runtime

    def execute(self, *, p=None):
        p = p or proposal()
        bound = InstrumentBoundTradeProposal(**vars(p), instrument=self.instrument)
        analysis = AgentAnalysisRequest("INFY", Market.INDIA, AssetClass.EQUITY, NOW, {})
        evidence = (AgentEvidence("synthetic-tech", AgentDomain.TECHNICAL, "INFY", Stance.BUY,
                      D(".8"), D(".02"), D(".03"), ("synthetic evidence",), NOW, 0),)
        req = make_request(self.broker, p=bound)
        return self.runtime._execute_proposal(analysis, evidence, bound, req.capital_plan,
                                               req.portfolio, None, "tenant")


def test_common_swarm_path_reaches_institutional_ledger_and_accounting_not_direct_submit(tmp_path, monkeypatch):
    h = BridgeHarness(tmp_path)
    try:
        monkeypatch.setattr(SwarmPaperTradingService, "_dispatch_approved",
                            lambda *a, **k: pytest.fail("no direct-submit fallback"))
        result = h.execute()
        assert result.fill is not None, result.risk_decision.reason
        assert result.order_state is OrderState.FILLED
        assert h.programs.db.execute("SELECT count(*) FROM execution_programs").fetchone()[0] == 1
        assert h.journal.native_balance("tenant", "CASH_AVAILABLE", "INR") == D(99000)
        assert h.journal.native_balance("tenant", "SECURITIES_COST", "INR") == D(1000)
        row = h.broker._connection.execute("SELECT payload FROM paper_decision_evidence").fetchone()
        saved = json.loads(row[0])
        assert saved["input_matrix"][0]["agent_id"] == "synthetic-tech"
        assert saved["order_id"] == result.fill.order_id == result.xai_trace.order_id
        assert saved["approved_order"]["strategy_id"] == "atlas-strategy"
        pid = saved["institutional_program"]
        assert saved["institutional_slice"] == 1
        assert h.programs.get(pid).state.value == "COMPLETE"
        context = h.programs.load_context(pid, tenant_id="tenant")
        assert context.request.proposal.provenance["institutional_source_trace"]["decision_id"] == result.proposal.decision_id
        assert context.request.proposal.provenance["institutional_input_source"] == "synthetic-inputs-v1"
        assert h.broker.reconcile("tenant")["status"] == "matched"
    finally:
        h.close()


def test_missing_required_evidence_never_falls_back_to_plain_swarm_execution(tmp_path, monkeypatch):
    h = BridgeHarness(tmp_path)
    try:
        h.inputs = replace(h.inputs, request_provider=lambda _: None)
        h.runtime = h.new_runtime()
        monkeypatch.setattr(SwarmPaperTradingService, "_dispatch_approved",
                            lambda *a, **k: pytest.fail("no direct-submit fallback"))
        result = h.execute()
        assert result.fill is None and result.risk_decision.reason == "institutional_request_evidence_missing"
        assert h.broker.ledger_entries("tenant") == ()
        assert h.programs.db.execute("SELECT count(*) FROM execution_programs").fetchone()[0] == 0
    finally:
        h.close()


def test_operating_preflight_is_rechecked_after_the_institutional_inputs(tmp_path):
    h = BridgeHarness(tmp_path)
    try:
        calls = []
        def preflight(_):
            calls.append(1)
            return None if len(calls) == 1 else "pilot_stale_entry_price"
        h.runtime.pre_submit_check = preflight
        result = h.execute()
        assert result.fill is None
        assert result.order_state is OrderState.SUBMISSION_UNCERTAIN
        assert result.risk_decision.reason.endswith("pilot_stale_entry_price")
        assert len(calls) == 2
        assert h.broker.ledger_entries("tenant") == ()
        assert h.programs.recovery_required("tenant")
    finally:
        h.close()


@pytest.mark.parametrize("fault", ["raises", "wrong_quantity", "wrong_time", "wrong_portfolio",
    "no_edge", "bad_edge", "no_volume", "twap", "plain_proposal"])
def test_bad_or_missing_institutional_inputs_hold_without_any_direct_fallback(tmp_path, fault):
    from datetime import timedelta

    from quant_ai.execution.planner import ExecutionAlgorithm
    h = BridgeHarness(tmp_path)
    try:
        def provider(packet):
            if fault == "raises":
                raise RuntimeError("synthetic source failure")
            value = h.request_provider(packet)
            if fault == "wrong_quantity":
                return replace(value, proposal=replace(value.proposal, quantity=20))
            if fault == "wrong_time":
                return replace(value, observed_at=NOW + timedelta(minutes=1))
            if fault == "wrong_portfolio":
                return replace(value, portfolio=replace(value.portfolio, equity=D(200000)))
            if fault == "no_edge":
                return replace(value, edge_evidence=None)
            if fault == "bad_edge":
                return replace(value, edge_evidence=replace(value.edge_evidence,
                    average_win_return=D(".001"), expected_cost_return=D(".002")))
            if fault == "no_volume":
                return replace(value, volume_buckets=())
            if fault == "twap":
                return replace(value, execution_algorithm=ExecutionAlgorithm.TWAP)
            return replace(value, proposal=proposal())
        h.inputs = replace(h.inputs, request_provider=provider)
        h.runtime = h.new_runtime()
        result = h.execute()
        assert result.fill is None and not result.risk_decision.approved
        assert h.broker.ledger_entries("tenant") == ()
        assert h.oms.all_orders("tenant") == ()
        assert h.programs.db.execute("SELECT count(*) FROM execution_programs").fetchone()[0] == 0
    finally:
        h.close()


@pytest.mark.parametrize("hook", ["snapshot_provider", "pre_submit_check"])
def test_required_operating_hooks_cannot_be_omitted(tmp_path, hook):
    h = BridgeHarness(tmp_path)
    try:
        setattr(h.runtime, hook, None)
        result = h.execute()
        assert result.fill is None and result.risk_decision.reason == "institutional_runtime_unavailable"
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        h.close()


@pytest.mark.parametrize("boundary", ["request_provider", "oms"])
def test_halt_activated_during_handoff_blocks_final_submission(tmp_path, monkeypatch, boundary):
    h = BridgeHarness(tmp_path)
    try:
        if boundary == "request_provider":
            def source(packet):
                h.runtime.kill_switch.engage("synthetic hold")
                return h.request_provider(packet)
            h.inputs = replace(h.inputs, request_provider=source)
            h.runtime = h.new_runtime()
        else:
            original = h.oms.submitted
            def submitted(*args, **kwargs):
                result = original(*args, **kwargs)
                h.runtime.kill_switch.engage("synthetic hold")
                return result
            monkeypatch.setattr(h.oms, "submitted", submitted)
        result = h.execute()
        assert result.fill is None and "kill_switch_engaged" in result.risk_decision.reason
        assert h.broker.ledger_entries("tenant") == ()
        assert h.programs.recovery_required("tenant")
    finally:
        h.close()


def test_committed_broker_exception_recovers_receipt_once_after_reconstruction(tmp_path, monkeypatch):
    h = BridgeHarness(tmp_path)
    try:
        submit = h.broker.submit_with_evidence
        def interrupted(*args, **kwargs):
            submit(*args, **kwargs)
            raise ValueError("synthetic interruption after broker commit")
        monkeypatch.setattr(h.broker, "submit_with_evidence", interrupted)
        result = h.execute()
        assert result.fill is None and result.order_state is OrderState.SUBMISSION_UNCERTAIN
        assert len(h.broker.ledger_entries("tenant")) == 1
        monkeypatch.setattr(h.broker, "submit_with_evidence", submit)
        pid = h.programs.db.execute("SELECT program_id FROM execution_programs").fetchone()[0]
        fresh = h.new_runtime()
        fresh._coordinator.restore_runtime_context(pid, tenant_id="tenant")
        assert fresh._coordinator.reconcile_accounting(pid).stage.value == "COMPLETE"
        assert fresh._coordinator.reconcile_accounting(pid).stage.value == "COMPLETE"
        h.runtime = fresh
        assert h.execute().fill is None
        assert len(h.broker.ledger_entries("tenant")) == 1
        assert h.journal.native_balance("tenant", "CASH_AVAILABLE", "INR") == D(99000)
    finally:
        h.close()


def test_accounting_failure_is_not_reported_as_completed_trading(tmp_path, monkeypatch):
    h = BridgeHarness(tmp_path)
    try:
        buy = h.accounting.buy_security
        monkeypatch.setattr(h.accounting, "buy_security",
                            lambda *a, **k: (_ for _ in ()).throw(ValueError("synthetic journal failure")))
        result = h.execute()
        assert result.fill is None and result.order_state is OrderState.SUBMISSION_UNCERTAIN
        assert len(h.broker.ledger_entries("tenant")) == 1
        pid = h.programs.db.execute("SELECT program_id FROM execution_programs").fetchone()[0]
        assert h.programs.get(pid).slices[0].state.value == "FILLED_UNACCOUNTED"
        monkeypatch.setattr(h.accounting, "buy_security", buy)
        assert h.runtime._coordinator.reconcile_accounting(pid).stage.value == "COMPLETE"
        assert h.journal.native_balance("tenant", "CASH_AVAILABLE", "INR") == D(99000)
    finally:
        h.close()


def test_covered_sell_retains_entry_evidence_exemptions_and_posts_correctly(tmp_path):
    h = BridgeHarness(tmp_path)
    try:
        assert h.execute().fill is not None
        h.runtime.kill_switch.engage("synthetic entry stop")
        def source(packet):
            return replace(h.request_provider(packet), edge_evidence=None,
                           strategy_opportunities=(), projected_factor_positions=())
        h.inputs = replace(h.inputs, request_provider=source)
        h.runtime = h.new_runtime()
        h.runtime.kill_switch.engage("synthetic entry stop")
        result = h.execute(p=proposal(side=Side.SELL, decision_id="exit"))
        assert result.fill is not None, result.risk_decision.reason
        assert h.broker.get_positions("tenant") == ()
        assert h.journal.native_balance("tenant", "SECURITIES_COST", "INR") == 0
        assert h.journal.native_balance("tenant", "CASH_AVAILABLE", "INR") == D(100000)
        # Closing exposure does not pretend the separate M01 release lifecycle exists.
        from quant_ai.execution.shared_risk import verify_shared_risk
        assert verify_shared_risk(h.programs.db, "tenant")["status"] == "consistent"
    finally:
        h.close()


def test_sync_and_async_public_swarm_apis_reuse_the_bridge(tmp_path):
    import asyncio

    from quant_ai.agents.swarm import InstrumentBoundAnalysisRequest
    h = BridgeHarness(tmp_path)
    try:
        analysis = InstrumentBoundAnalysisRequest("INFY", Market.INDIA, AssetClass.EQUITY,
                                                  NOW, {}, instrument=h.instrument)
        evidence = tuple(AgentEvidence(f"synthetic-{i}", AgentDomain.TECHNICAL,
            "INFY", Stance.STRONG_BUY, D(".8"), D(".02"), D(".01"),
            ("synthetic only",), NOW, 0) for i in range(4))
        req = make_request(h.broker)
        kwargs = {"quantity": 10, "reference_price": D(100), "stop_price": D(95),
                  "take_profit_price": D(110), "country": "India", "tenant_id": "tenant"}
        result = h.runtime.execute(analysis, evidence, req.capital_plan, req.portfolio, **kwargs)
        assert result.fill is not None, result.risk_decision.reason
        result2 = asyncio.run(h.runtime.execute_async(analysis, evidence, req.capital_plan,
                                                     snapshot_from_broker(h.broker), **kwargs))
        assert result2.fill is None and result2.risk_decision.reason == "position_already_open"
        assert len(h.broker.ledger_entries("tenant")) == 1
    finally:
        h.close()


def test_explicit_runtime_factory_keeps_default_route_and_records_selected_policy(tmp_path):
    from quant_ai.agents.traded_runtime import build_traded_runtime, runtime_configuration
    h = BridgeHarness(tmp_path)
    try:
        direct = build_traded_runtime(broker=h.broker, oms=h.oms)
        selected = build_traded_runtime(broker=h.broker, oms=h.oms, institutional_inputs=h.inputs)
        assert type(direct) is SwarmPaperTradingService
        assert type(selected) is InstitutionalSwarmPaperTradingService
        info = runtime_configuration(selected)
        assert info["components"]["runtime"]["parameters"]["institutional_configuration"]["sourceRevision"] == "synthetic-inputs-v1"
        assert "unsupported_component:quant_ai.agents.institutional_runtime.InstitutionalSwarmPaperTradingService" not in info["componentIssues"]
        before = runtime_configuration(selected)
        selected._coordinator.shared_risk_policy = replace(h.inputs.shared_risk_policy, revision="other")
        assert before != runtime_configuration(selected)
    finally:
        h.close()


def test_paper_mode_change_during_inputs_never_reaches_submission(tmp_path, monkeypatch):
    h = BridgeHarness(tmp_path)
    try:
        def source(packet):
            monkeypatch.setenv("TRADING_LIVE_MONEY_ACTIVE", "true")
            return h.request_provider(packet)
        h.inputs = replace(h.inputs, request_provider=source)
        h.runtime = h.new_runtime()
        result = h.execute()
        assert result.fill is None
        assert result.risk_decision.reason.endswith("institutional_runtime_paper_only")
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        h.close()


def test_changed_trace_cannot_replace_the_approved_request_evidence(tmp_path):
    h = BridgeHarness(tmp_path)
    try:
        result = h.execute()
        assert result.fill is not None
        row = json.loads(h.broker._connection.execute("SELECT payload FROM paper_decision_evidence").fetchone()[0])
        pid = row["institutional_program"]
        source = h.programs.load_context(pid, tenant_id="tenant").request.proposal.provenance["institutional_source_trace"]
        source["declared_rationales"] = ["fabricated evidence"]
        with pytest.raises(ValueError, match="institutional_decision_trace_identity_mismatch"):
            h.runtime._coordinator.execute_due(pid, now=NOW, decision_trace=source)
        assert len(h.broker.ledger_entries("tenant")) == 1
    finally:
        h.close()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_actual_analysis_pipeline_reaches_institutional_accounting_with_supplied_inputs(tmp_path, asynchronous):
    from datetime import datetime, timezone

    from quant_ai.execution.planner import VolumeBucket
    from quant_ai.intelligence.pipeline import SwarmMarketAnalysisPipeline
    from quant_ai.intelligence.sandbox import (
        SandboxFundamentalDataProvider,
        SandboxMacroIndicatorProvider,
        SandboxNewsSentimentProvider,
    )
    from quant_ai.marketdata.feed import IndiaSandboxMarketDataFeed
    h = BridgeHarness(tmp_path)
    try:
        def source(packet):
            return replace(h.request_provider(packet), volume_buckets=(VolumeBucket(packet.analysis.observed_at, 10000),))
        h.inputs = replace(h.inputs, request_provider=source,
                           slice_volume_provider=lambda *_: 10000)
        h.runtime = h.new_runtime()
        class SyntheticFundamentals(SandboxFundamentalDataProvider):
            def fetch(self, subject, now):
                row = super().fetch("TCS", now)
                return replace(row, subject=subject)
        class SyntheticNews(SandboxNewsSentimentProvider):
            def fetch(self, subject, now):
                return tuple(replace(row, subject=subject) for row in super().fetch("TCS", now))
        pipeline = SwarmMarketAnalysisPipeline(IndiaSandboxMarketDataFeed(base_price=D(100)),
            SyntheticNews(), SyntheticFundamentals(),
            SandboxMacroIndicatorProvider(), runtime=h.runtime, bind_order_instruments=True)
        request = make_request(h.broker)
        now = datetime.now(timezone.utc)
        if asynchronous:
            import asyncio
            result = asyncio.run(pipeline.run_async(h.instrument, now, request.capital_plan,
                request.portfolio, quantity=2, country="India", tenant_id="tenant"))
        else:
            result = pipeline.run(h.instrument, now, request.capital_plan, request.portfolio,
                                  quantity=2, country="India", tenant_id="tenant")
        assert result.execution.fill is not None, result.execution.risk_decision.reason
        assert h.programs.db.execute("SELECT count(*) FROM execution_programs").fetchone()[0] == 1
        assert h.journal.verify("tenant")["verified"] is True
        saved = json.loads(h.broker._connection.execute("SELECT payload FROM paper_decision_evidence").fetchone()[0])
        assert len(saved["input_matrix"]) == len(result.evidence) > 1
        assert h.journal.native_balance("tenant", "SECURITIES_COST", "INR") > 0
        assert h.broker.reconcile("tenant")["status"] == "matched"
    finally:
        h.close()


def test_actual_runner_factory_selects_institutional_route_and_live_preflight(tmp_path):
    from types import SimpleNamespace

    from quant_ai.daemon import build_ghost_runner
    from quant_ai.governance.directives import FounderDirectives
    (tmp_path / "components").mkdir()
    h = BridgeHarness(tmp_path / "components")
    runner = None
    try:
        runner = build_ghost_runner(zerodha_api_key="synthetic", zerodha_access_token="synthetic",
            zerodha_instrument_tokens=(1,), zerodha_symbol_by_token={1: "INFY"},
            ib_client=SimpleNamespace(), ib_contracts=(), include_ibkr=False,
            database=tmp_path / "runner-paper.sqlite", tenant_id="tenant",
            oms_database=tmp_path / "runner-oms.sqlite",
            log_path=tmp_path / "events.jsonl", xai_directory=tmp_path / "proofs",
            halt_file=tmp_path / "HALT", directives=FounderDirectives(watchlist=(h.instrument,)),
            pilot_mode=True, order_identity_mode="bound_v1", institutional_inputs=h.inputs)
        daemon = runner.daemon
        runtime = daemon.scheduler.pipeline.runtime
        assert type(runtime) is InstitutionalSwarmPaperTradingService
        assert runtime._coordinator.broker is runtime.broker is daemon.tracker.broker
        assert runtime._coordinator.oms is runtime.oms
        assert runtime._coordinator.warden is runtime.warden
        assert runtime.pre_submit_check.__self__ is daemon
        assert runtime.pre_submit_check.__func__ is daemon._pilot_pre_submit.__func__
        assert runtime.snapshot_provider().equity == D(100000)
        assert runtime.broker.ledger_entries("tenant") == ()
        # No provider session or daemon loop is started. Missing quotes retain the
        # real pilot refusal, rather than an invented successful operating check.
        candidate = InstrumentBoundTradeProposal(**vars(proposal()), instrument=h.instrument)
        assert runtime.pre_submit_check(candidate) is not None
        assert h.programs.db.execute("SELECT count(*) FROM execution_programs").fetchone()[0] == 0
    finally:
        if runner is not None:
            runtime = runner.daemon.scheduler.pipeline.runtime
            runtime.oms.close()
            runtime.broker.close()
        h.close()


@pytest.mark.parametrize("field,value", [("request_provider", None), ("source_revision", ""),
    ("source_revision", " ambiguous "), ("shared_risk_policy", None), ("edge_policy", None)])
def test_required_institutional_configuration_is_not_defaulted(tmp_path, field, value):
    h = BridgeHarness(tmp_path)
    try:
        with pytest.raises(ValueError, match="institutional_runtime_inputs_invalid"):
            replace(h.inputs, **{field: value})
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        h.close()


@pytest.mark.parametrize("field", ["factor_position_provider", "strategy_exposure_provider",
                                   "slice_volume_provider"])
def test_replacing_selected_coordinator_provider_is_detected_before_execution(tmp_path, field):
    h = BridgeHarness(tmp_path)
    try:
        setattr(h.runtime._coordinator, field, lambda *_: None)
        result = h.execute()
        assert result.fill is None and result.risk_decision.reason == "institutional_runtime_unavailable"
        assert h.broker.ledger_entries("tenant") == ()
        assert h.programs.db.execute("SELECT count(*) FROM execution_programs").fetchone()[0] == 0
    finally:
        h.close()


def test_provider_cannot_mutate_the_original_portfolio_and_hide_the_change(tmp_path):
    h = BridgeHarness(tmp_path)
    try:
        def source(packet):
            packet.portfolio.symbol_quantity["HIDDEN"] = 100
            return h.request_provider(packet)
        h.inputs = replace(h.inputs, request_provider=source)
        h.runtime = h.new_runtime()
        result = h.execute()
        assert result.fill is None and result.risk_decision.reason == "institutional_request_identity_mismatch"
        assert h.broker.get_positions("tenant") == ()
        assert h.broker.ledger_entries("tenant") == ()
    finally:
        h.close()


def test_independent_protection_does_not_require_the_bridge_source_provider(tmp_path):
    from quant_ai.execution.protective_exits import ProtectiveExitEngine
    h = BridgeHarness(tmp_path)
    try:
        assert h.execute().fill is not None
        def unavailable(*_):
            pytest.fail("Independent protection must not consult institutional entry inputs")
        h.inputs = replace(h.inputs, request_provider=unavailable)
        h.runtime = h.new_runtime()
        exits = ProtectiveExitEngine(h.broker, lambda _: D(90), tenant_id="tenant").evaluate()
        assert len(exits) == 1 and exits[0].filled
        assert h.broker.get_positions("tenant") == ()
        assert h.runtime._coordinator.reconcile_protective_accounting(currency="INR").status == "matched"
        assert h.journal.native_balance("tenant", "SECURITIES_COST", "INR") == 0
        assert len(h.broker.ledger_entries("tenant")) == 2
    finally:
        h.close()


def test_simultaneous_bridge_calls_for_one_decision_do_not_double_submit(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    h = BridgeHarness(tmp_path)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: h.execute(), range(2)))
        assert sum(result.fill is not None for result in results) == 1
        assert len(h.broker.ledger_entries("tenant")) == 1
        assert h.programs.db.execute("SELECT count(*) FROM execution_programs").fetchone()[0] == 1
        assert h.journal.native_balance("tenant", "SECURITIES_COST", "INR") == D(1000)
    finally:
        h.close()


@pytest.mark.parametrize("veto", [False, "", {"approved": True}])
def test_ambiguous_last_submission_guard_result_is_not_interpreted_as_approval(tmp_path, veto):
    h = BridgeHarness(tmp_path)
    try:
        count = 0
        def preflight(_):
            nonlocal count
            count += 1
            return None if count == 1 else veto
        h.runtime.pre_submit_check = preflight
        result = h.execute()
        assert result.fill is None
        assert result.risk_decision.reason.endswith("institutional_pre_submit_guard_invalid")
        assert h.broker.ledger_entries("tenant") == ()
        assert h.programs.recovery_required("tenant")
    finally:
        h.close()
