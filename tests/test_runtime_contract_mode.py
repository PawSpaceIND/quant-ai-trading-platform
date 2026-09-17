"""Synthetic daemon assembly and persistence tests; no network or live order calls."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from test_pilot_closure import INSTRUMENT, publish_tick
from test_runtime_manifest import setup

from quant_ai.daemon import build_ghost_runner
from quant_ai.domain.models import Market, OrderIntent, PortfolioSnapshot, Side
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.governance.directives import FounderDirectives

D = Decimal
NOW = datetime(2026, 9, 15, 6, tzinfo=timezone.utc)


def build(tmp_path, **changes):
    args = {
        "zerodha_api_key": "synthetic-key", "zerodha_access_token": "synthetic-token",
        "zerodha_instrument_tokens": (1,), "zerodha_symbol_by_token": {1: "INFY"},
        "ib_client": SimpleNamespace(), "ib_contracts": (), "include_ibkr": False,
        "database": tmp_path / "ledger.db", "tenant_id": "pilot", "log_path": tmp_path / "events.jsonl",
        "xai_directory": tmp_path / "proofs", "halt_file": tmp_path / "HALT",
        "directives": FounderDirectives(watchlist=(INSTRUMENT,)), "pilot_mode": True,
        "order_identity_mode": "bound_v1", "oms_database": tmp_path / "oms.sqlite",
    }
    args.update(changes)
    return build_ghost_runner(**args)


def close(runner):
    runtime = runner.daemon.scheduler.pipeline.runtime
    if runtime.oms is not None:
        runtime.oms.close()
    runtime.broker.close()


def test_manifest_detects_instrument_mode_drift(tmp_path, monkeypatch):
    runner, _ = setup(tmp_path, monkeypatch)
    try:
        first = runner.daemon.strategy_manifest.check()
        runner.daemon.scheduler.pipeline.bind_order_instruments = True
        after = runner.daemon.strategy_manifest.check()
        assert after["sha256"] != first["sha256"]
        assert after["status"] == "changed"
    finally:
        close(runner)


def test_bound_builder_arms_pipeline_oms_and_retained_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("PRAMANA_RELEASE_REVISION", "a" * 40)
    runner = build(tmp_path)
    try:
        p = runner.daemon.scheduler.pipeline
        assert p.bind_order_instruments is True
        assert p.runtime.oms.path == tmp_path / "oms.sqlite"
        captured = runner.daemon.strategy_manifest.capture()
        assert captured["issues"] == []
        assert captured["manifest"]["order_identity"]["mode"] == "bound_v1"
        assert captured["manifest"]["components"]["pipeline"]["parameters"]["bind_order_instruments"] is True
        raw = json.dumps(captured)
        assert "synthetic-key" not in raw and "synthetic-token" not in raw
        assert str(tmp_path) not in raw
    finally:
        close(runner)


@pytest.mark.parametrize("mode", ["", "true", "BOUND_V1", "bound_v2", True, 1, None])
def test_invalid_modes_fail_before_database_creation(tmp_path, mode):
    with pytest.raises((TypeError, ValueError), match="order_identity_mode"):
        build(tmp_path, order_identity_mode=mode)
    assert not (tmp_path / "ledger.db").exists()
    assert not (tmp_path / "oms.sqlite").exists()


@pytest.mark.parametrize("defect", ["missing_oms", "same_path", "memory_oms", "memory_paper", "nonpilot"])
def test_bound_rollout_refuses_unsafe_storage_or_scope(tmp_path, defect):
    changes = {
        "missing_oms": {"oms_database": None},
        "same_path": {"oms_database": tmp_path / "ledger.db"},
        "memory_oms": {"oms_database": ":memory:"},
        "memory_paper": {"database": ":memory:"},
        "nonpilot": {"pilot_mode": False},
    }[defect]
    with pytest.raises((TypeError, ValueError), match="runtime_identity_"):
        build(tmp_path, **changes)
    assert not (tmp_path / "ledger.db").exists()


def test_legacy_default_is_not_silently_migrated(tmp_path):
    runner = build(tmp_path, order_identity_mode="legacy_cash", oms_database=None)
    try:
        assert runner.daemon.scheduler.pipeline.bind_order_instruments is False
        assert runner.daemon.scheduler.pipeline.runtime.oms is None
    finally:
        close(runner)


@pytest.mark.parametrize("held", [True, False])
def test_legacy_history_cannot_be_relabelled_bound(tmp_path, held):
    broker = PaperBrokerService(tmp_path / "ledger.db", slippage_bps=D(0))
    order = OrderIntent("INFY", Market.INDIA, Side.BUY, 1, D(100), "legacy", tenant_id="pilot", stop_price=D(95))
    broker.buy(order)
    if not held:
        broker.sell(replace(order, side=Side.SELL))
    before = tuple(broker._connection.iterdump())
    broker.close()
    with pytest.raises(ValueError, match="runtime_identity_legacy_history_requires_review"):
        build(tmp_path)
    broker = PaperBrokerService(tmp_path / "ledger.db")
    try:
        assert tuple(broker._connection.iterdump()) == before
    finally:
        broker.close()


@pytest.mark.parametrize("change", ["mode", "oms", "scope"])
def test_restart_cannot_silently_change_pinned_configuration(tmp_path, change):
    runner = build(tmp_path)
    before = tuple(runner.daemon.tracker.broker._connection.iterdump())
    close(runner)
    updates = {
        "mode": {"order_identity_mode": "legacy_cash", "oms_database": None},
        "oms": {"oms_database": tmp_path / "other-oms.sqlite"},
        "scope": {"directives": FounderDirectives(watchlist=(replace(INSTRUMENT, metadata={"source": "changed"}),))},
    }[change]
    with pytest.raises(ValueError, match="runtime_identity_configuration_change_requires_review"):
        build(tmp_path, **updates)
    broker = PaperBrokerService(tmp_path / "ledger.db")
    try:
        assert tuple(broker._connection.iterdump()) == before
    finally:
        broker.close()


def test_bound_scope_enforces_direct_submission_even_after_restart(tmp_path):
    runner = build(tmp_path)
    close(runner)
    broker = PaperBrokerService(tmp_path / "ledger.db", slippage_bps=D(0))
    try:
        before = tuple(broker._connection.iterdump())
        with pytest.raises(ValueError, match="runtime_identity_bound_order_required"):
            broker.buy(OrderIntent("INFY", Market.INDIA, Side.BUY, 1, D(100), "bypass", tenant_id="pilot", stop_price=D(95)))
        assert tuple(broker._connection.iterdump()) == before
    finally:
        broker.close()


def test_real_factory_retains_bound_order_and_manifest_in_oms_and_fill(tmp_path, monkeypatch):
    from quant_ai.agents.contracts import Stance
    from quant_ai.agents.swarm import AtlasCIOAgent, InstrumentBoundTradeProposal
    from quant_ai.instruments.identity import canonical_instrument_identity
    monkeypatch.setenv("PRAMANA_RELEASE_REVISION", "a" * 40)
    runner = build(tmp_path)
    try:
        d = runner.daemon
        p = d.scheduler.pipeline
        d.clock = lambda: NOW
        publish_tick(runner, "100", NOW)
        analysis = p._analysis_request(INSTRUMENT, NOW, {}, 0)
        decision = SimpleNamespace(action=Stance.BUY, cycle_id="bound-daemon-proof", confidence=D('.8'),
                                   expected_return=D('.02'), expected_risk=D('.01'), rationale=("synthetic",), provenance=None)
        proposal = AtlasCIOAgent._proposal_from_decision(analysis, decision, quantity=1,
                    reference_price=D(100), stop_price=D(95), take_profit_price=D(110), country="INDIA")
        assert isinstance(proposal, InstrumentBoundTradeProposal)
        result = p.runtime._execute_proposal(analysis, (), proposal, d.plan,
                    PortfolioSnapshot(D(100000), D(0), D(0)), None, "pilot")
        assert result.fill is not None, result.risk_decision.reason
        client_id = p.runtime.oms.client_order_id(result.risk_decision.order, proposal.decision_id)
        assert p.runtime.oms.get_intent(client_id).instrument == INSTRUMENT
        assert p.runtime.oms.verify(client_id)["verified"]
        ledger = p.runtime.broker.ledger_entries("pilot")[0]
        assert ledger.instrument_identity == canonical_instrument_identity(INSTRUMENT)
        evidence = json.loads(p.runtime.broker._connection.execute("SELECT payload FROM paper_decision_evidence").fetchone()[0])
        assert evidence["provenance"]["runtime_strategy"]["status"] == "matched"
        # Drift must halt new work, not suppress the independent covered exit.
        p.bind_order_instruments = False
        assert d._pilot_pre_submit(SimpleNamespace(side=Side.BUY)) == "pilot_strategy_manifest_unverified"
        publish_tick(runner, "90", NOW)
        d.protection_tick(NOW)
        assert not p.runtime.broker.get_positions("pilot")
        assert len(p.runtime.broker.ledger_entries("pilot")) == 2
    finally:
        close(runner)


def test_bound_mode_refuses_incomplete_release_manifest(tmp_path, monkeypatch):
    monkeypatch.delenv("PRAMANA_RELEASE_REVISION", raising=False)
    runner = build(tmp_path)
    try:
        runner.daemon.clock = lambda: NOW
        publish_tick(runner, "100", NOW)
        assert runner.daemon.strategy_manifest.summary["status"] == "incomplete"
        assert runner.daemon._pilot_pre_submit(SimpleNamespace(side=Side.BUY, symbol="INFY", reference_price=D(100))) == "pilot_strategy_manifest_unverified"
        assert runner.daemon.kill_switch.engaged
        assert not runner.daemon.tracker.broker.ledger_entries("pilot")
    finally:
        close(runner)


def test_bound_restart_holds_unresolved_oms_without_disabling_protection(tmp_path, monkeypatch):
    from quant_ai.domain.models import InstrumentBoundOrderIntent
    monkeypatch.setenv("PRAMANA_RELEASE_REVISION", "a" * 40)
    runner = build(tmp_path)
    runtime = runner.daemon.scheduler.pipeline.runtime
    order = InstrumentBoundOrderIntent("INFY", Market.INDIA, Side.BUY, 1, D(100), "test",
                tenant_id="pilot", stop_price=D(95), instrument=INSTRUMENT)
    row = runtime.oms.create(order, decision_id="interrupted", now=NOW)
    runtime.oms.approve_risk(row.client_order_id, now=NOW)
    runtime.oms.submission_uncertain(row.client_order_id, reason="synthetic interruption", now=NOW)
    close(runner)
    runner = build(tmp_path)
    try:
        runner.daemon.clock = lambda: NOW
        publish_tick(runner, "100", NOW)
        reason = runner.daemon._pilot_pre_submit(SimpleNamespace(side=Side.BUY, symbol="INFY", reference_price=D(100)))
        assert reason == "runtime_identity_oms_recovery_required"
        assert runner.daemon.kill_switch.engaged
        assert not runner.daemon.tracker.broker.ledger_entries("pilot")
    finally:
        close(runner)


@pytest.mark.parametrize("mode", ["", "bound", "true"])
def test_environment_mode_validation_precedes_provider_construction(tmp_path, monkeypatch, mode):
    from quant_ai.daemon import build_ghost_runner_from_env
    monkeypatch.setenv("PRAMANA_ORDER_IDENTITY_MODE", mode)
    monkeypatch.setenv("PRAMANA_PAPER_DB", str(tmp_path / "paper.sqlite"))
    def forbidden(*args, **kwargs):
        pytest.fail("invalid mode reached provider construction")
    monkeypatch.setattr("quant_ai.daemon.import_module", forbidden)
    with pytest.raises(ValueError, match="order_identity_mode"):
        build_ghost_runner_from_env()
    assert not (tmp_path / "paper.sqlite").exists()


@pytest.mark.parametrize("mode", ["legacy_cash", "bound_v1"])
def test_environment_wires_bound_mode_and_selected_oms(tmp_path, monkeypatch, mode):
    from quant_ai.daemon import build_ghost_runner_from_env
    for key,value in {
        "TRADING_LIVE_MONEY_ACTIVE":"false", "PRAMANA_ORDER_IDENTITY_MODE":mode,
        "PRAMANA_OMS_DB":str(tmp_path / "oms.sqlite") if mode == "bound_v1" else "", "PRAMANA_PAPER_DB":str(tmp_path / "ledger.db"),
        "PRAMANA_RELEASE_REVISION":"a" * 40, "PRAMANA_PILOT_MODE":"true", "PRAMANA_IBKR_ENABLED":"false",
        "PRAMANA_TARGET_SYMBOL":"INFY", "PRAMANA_TARGET_MARKET":"INDIA", "PRAMANA_TARGET_ASSET_CLASS":"EQUITY",
        "PRAMANA_TARGET_CURRENCY":"INR", "PRAMANA_TARGET_EXCHANGE":"NSE",
        "PRAMANA_GHOST_LOG":str(tmp_path / "events.jsonl"), "PRAMANA_XAI_DIR":str(tmp_path / "proofs"),
        "ZERODHA_API_KEY":"synthetic-key", "ZERODHA_ACCESS_TOKEN":"synthetic-token",
        "PRAMANA_ZERODHA_TOKENS_JSON":"[1]", "PRAMANA_ZERODHA_SYMBOLS_JSON":'{"1":"INFY"}',
        "PRAMANA_TENANT_ID":"pilot", "PRAMANA_DAILY_HISTORY_PROVIDER":"none",
    }.items():
        monkeypatch.setenv(key,value)
    monkeypatch.setattr("quant_ai.daemon.import_module", lambda _: SimpleNamespace(IB=SimpleNamespace))
    monkeypatch.setattr("quant_ai.daemon.AnthropicSwarmClient", lambda **kw: None)
    runner = build_ghost_runner_from_env()
    try:
        assert runner.daemon.scheduler.pipeline.bind_order_instruments is (mode == "bound_v1")
        if mode == "bound_v1":
            assert runner.daemon.scheduler.pipeline.runtime.oms.path == tmp_path / "oms.sqlite"
        else:
            assert runner.daemon.scheduler.pipeline.runtime.oms is None
        # Providers unavailable/unsupported are not magically qualified by this rollout.
        assert runner.daemon.strategy_manifest.summary["status"] in {"matched", "incomplete"}
    finally:
        close(runner)


def submit_bound(runner, decision_id="synthetic-bound"):
    from quant_ai.agents.contracts import Stance
    from quant_ai.agents.swarm import AtlasCIOAgent
    d = runner.daemon
    d.clock = lambda: NOW
    publish_tick(runner, "100", NOW)
    p = d.scheduler.pipeline
    analysis = p._analysis_request(INSTRUMENT, NOW, {}, 0)
    decision = SimpleNamespace(action=Stance.BUY, cycle_id=decision_id, confidence=D('.8'),
                expected_return=D('.02'), expected_risk=D('.01'), rationale=("synthetic",), provenance=None)
    proposal = AtlasCIOAgent._proposal_from_decision(analysis, decision, quantity=1,
                reference_price=D(100), stop_price=D(95), take_profit_price=D(110), country="INDIA")
    return p.runtime._execute_proposal(analysis, (), proposal, d.plan,
                PortfolioSnapshot(D(100000), D(0), D(0)), None, "pilot")


def test_same_path_empty_oms_cannot_hide_missing_execution_history(tmp_path, monkeypatch):
    from quant_ai.orders.oms import DurableOms
    monkeypatch.setenv("PRAMANA_RELEASE_REVISION", "a" * 40)
    runner = build(tmp_path)
    try:
        assert submit_bound(runner).fill is not None
        runtime = runner.daemon.scheduler.pipeline.runtime
        runtime.oms.close()
        (tmp_path / "oms.sqlite").rename(tmp_path / "retained-oms.sqlite")
        runtime.oms = DurableOms(tmp_path / "oms.sqlite")
        assert runner.daemon._pilot_pre_submit(SimpleNamespace(side=Side.BUY)) == "runtime_identity_oms_recovery_required"
        publish_tick(runner, "90", NOW)
        runner.daemon.protection_tick(NOW)
        assert not runtime.broker.get_positions("pilot")
        assert len(runtime.broker.ledger_entries("pilot")) == 2
    finally:
        close(runner)


@pytest.mark.parametrize("defect", ["missing", "different_path"])
def test_manifest_detects_durable_oms_wiring_changes(tmp_path, monkeypatch, defect):
    from quant_ai.orders.oms import DurableOms
    monkeypatch.setenv("PRAMANA_RELEASE_REVISION", "a" * 40)
    runner = build(tmp_path)
    try:
        runtime = runner.daemon.scheduler.pipeline.runtime
        runtime.oms.close()
        runtime.oms = None if defect == "missing" else DurableOms(tmp_path / "other.sqlite")
        result = runner.daemon.strategy_manifest.check()
        assert result["status"] == "changed"
        assert "runtime_identity_wiring_mismatch" in result["issues"]
    finally:
        close(runner)


def test_new_paper_book_cannot_adopt_unrelated_oms_history(tmp_path):
    from quant_ai.orders.oms import DurableOms
    with DurableOms(tmp_path / "oms.sqlite") as oms:
        oms.create(OrderIntent("INFY", Market.INDIA, Side.BUY, 1, D(100), "old", tenant_id="pilot"), decision_id="old", now=NOW)
    with pytest.raises(ValueError, match="runtime_identity_existing_oms_requires_review"):
        build(tmp_path)


def test_rollout_pin_is_append_only(tmp_path):
    import sqlite3
    runner = build(tmp_path)
    try:
        db = runner.daemon.tracker.broker._connection
        for query in ("DELETE FROM paper_runtime_order_identity", "UPDATE paper_runtime_order_identity SET payload='{}'"):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"), db:
                db.execute(query)
    finally:
        close(runner)


@pytest.mark.parametrize("alias", ["symlink", "hardlink"])
def test_oms_cannot_alias_broker_file(tmp_path, alias):
    import os
    broker = PaperBrokerService(tmp_path / "ledger.db")
    broker.close()
    if alias == "symlink":
        (tmp_path / "oms.sqlite").symlink_to(tmp_path / "ledger.db")
    else:
        os.link(tmp_path / "ledger.db", tmp_path / "oms.sqlite")
    with pytest.raises(ValueError, match="runtime_identity_"):
        build(tmp_path)


def test_post_commit_exception_is_uncertain_not_rejected(tmp_path, monkeypatch):
    from quant_ai.orders.state import OrderState
    monkeypatch.setenv("PRAMANA_RELEASE_REVISION", "a" * 40)
    runner = build(tmp_path)
    try:
        runtime = runner.daemon.scheduler.pipeline.runtime
        original = runtime.broker.submit_with_evidence
        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise ValueError("synthetic_error_after_commit")
        with monkeypatch.context() as patch:
            patch.setattr(runtime.broker, "submit_with_evidence", interrupted)
            result = submit_bound(runner, "post-commit")
        assert len(runtime.broker.ledger_entries("pilot")) == 1
        assert result.order_state is OrderState.SUBMISSION_UNCERTAIN
        assert runtime.oms.all_orders("pilot")[0].state is OrderState.SUBMISSION_UNCERTAIN
        assert runner.daemon._pilot_pre_submit(SimpleNamespace(side=Side.BUY)) == "runtime_identity_oms_recovery_required"
        publish_tick(runner, "90", NOW)
        runner.daemon.protection_tick(NOW)
        assert not runtime.broker.get_positions("pilot")
    finally:
        close(runner)


def test_compose_passes_explicit_mode_without_changing_the_default():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    compose = (root / "deploy/docker-compose.yml").read_text()
    ghost = compose.split("  pramana-ghost:", 1)[1].split("  dashboard:", 1)[0]
    assert "PRAMANA_ORDER_IDENTITY_MODE: ${PRAMANA_ORDER_IDENTITY_MODE-legacy_cash}" in ghost
    assert "PRAMANA_OMS_DB: ${PRAMANA_OMS_DB:-}" in ghost
    assert "PRAMANA_ORDER_IDENTITY_MODE=legacy_cash" in (root / ".env.example").read_text()
