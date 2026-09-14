import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from test_pilot_closure import publish_tick, runner_for

from quant_ai.domain.models import Market, OrderIntent, Side
from quant_ai.governance.runtime_manifest import export_manifest
from quant_ai.intelligence.external.rss import RssNewsSentimentAdapter
from quant_ai.intelligence.resilience import ResilientHttpClient, UrllibTransport


def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("PRAMANA_RELEASE_REVISION", "a" * 40)
    runner = runner_for(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    (source / "strategy.py").write_text("synthetic_source = 1\n")
    runner.daemon.bind_strategy_manifest(runner.streams, source_root=source, revision="a" * 40)
    assert runner.daemon.strategy_manifest.summary["status"] == "matched", (
        runner.daemon.strategy_manifest.summary
    )
    return runner, source


def test_effective_configuration_is_stable_but_changes_invalidate_binding(tmp_path, monkeypatch):
    runner, _ = setup(tmp_path, monkeypatch)
    monitor = runner.daemon.strategy_manifest
    first = monitor.check()
    assert monitor.check()["sha256"] == first["sha256"]
    runner.daemon.plan = replace(runner.daemon.plan, max_position_fraction=Decimal(".04"))
    changed = monitor.check()
    assert changed["status"] == "changed"
    assert changed["sha256"] != first["sha256"]
    assert changed["bootSha256"] == first["sha256"]


def test_source_mutation_is_checked_before_submission_and_halts_entries(tmp_path, monkeypatch):
    runner, source = setup(tmp_path, monkeypatch)
    (source / "strategy.py").write_text("synthetic_source = 2\n")
    reason = runner.daemon._pilot_pre_submit(SimpleNamespace(side=Side.BUY))
    assert reason == "pilot_strategy_manifest_unverified"
    assert runner.daemon.kill_switch.engaged
    assert runner.daemon.strategy_manifest.summary["status"] == "changed"
    assert not runner.daemon.tracker.broker.ledger_entries("pilot")
    assert runner.daemon.tracker.risk_state.kill_switch_state("pilot")[0]
    runner.daemon.tracker.broker.close()
    restarted = runner_for(tmp_path)
    assert restarted.daemon.kill_switch.engaged


def test_configuration_halt_does_not_suppress_protective_exit(tmp_path, monkeypatch):
    runner, _ = setup(tmp_path, monkeypatch)
    broker = runner.daemon.tracker.broker
    broker.buy(
        OrderIntent(
            "INFY",
            Market.INDIA,
            Side.BUY,
            2,
            Decimal(100),
            "synthetic",
            tenant_id="pilot",
            stop_price=Decimal(95),
        )
    )
    runner.daemon.scheduler.pipeline.runtime.max_open_positions = 2
    now = datetime.now(timezone.utc)
    publish_tick(runner, "90", now)
    runner.daemon.protection_tick(now)
    assert runner.daemon.kill_switch.engaged
    assert not broker.get_positions("pilot")
    runtime = json.loads(
        broker._connection.execute(
            "SELECT payload FROM pilot_runtime WHERE tenant_id='pilot'"
        ).fetchone()[0]
    )
    assert runtime["strategyManifest"]["status"] == "changed"
    assert runtime["halted"]


def test_credentials_do_not_enter_manifest_or_change_its_fingerprint(tmp_path, monkeypatch):
    runner, _ = setup(tmp_path, monkeypatch)
    monitor = runner.daemon.strategy_manifest
    before = monitor.capture()["sha256"]
    runner.streams[0].api_key = "UNIQUE_SECRET_KEY_NEVER_EXPORT"
    runner.streams[0].access_token = "UNIQUE_SECRET_TOKEN_NEVER_EXPORT"
    after = monitor.capture()
    assert before == after["sha256"]
    assert "UNIQUE_SECRET" not in json.dumps(after)
    http = ResilientHttpClient(UrllibTransport())
    runner.daemon.scheduler.pipeline.news = RssNewsSentimentAdapter(
        http, ("https://example.test/feed?api_key=UNIQUE_SECRET&q=INFY",)
    )
    first = monitor.capture()
    runner.daemon.scheduler.pipeline.news.feed_urls = (
        "https://example.test/feed?api_key=ROTATED_SECRET&q=INFY",
    )
    assert monitor.capture()["sha256"] == first["sha256"]
    assert "UNIQUE_SECRET" not in json.dumps(first)
    runner.daemon.scheduler.pipeline.news.feed_urls = ("https://example.test/feed?q=TCS",)
    assert monitor.capture()["sha256"] != first["sha256"]


def test_unknown_component_and_unconfigured_release_remain_incomplete(tmp_path, monkeypatch):
    runner, source = setup(tmp_path, monkeypatch)
    runner.daemon.scheduler.pipeline.sizer = SimpleNamespace(policy="unknown")
    runner.daemon.bind_strategy_manifest(runner.streams, source_root=source, revision="")
    summary = runner.daemon.strategy_manifest.summary
    assert summary["status"] == "incomplete"
    assert "release_revision_unconfigured" in summary["issues"]
    assert any("unsupported_component" in issue for issue in summary["issues"])


def test_export_is_exact_hash_bound_private_and_refuses_tampering_or_overwrite(
    tmp_path, monkeypatch
):
    runner, _ = setup(tmp_path, monkeypatch)
    sha = runner.daemon.strategy_manifest.summary["sha256"]
    ledger = tmp_path / "ledger.db"
    output = tmp_path / "manifest.json"
    assert export_manifest(ledger, "pilot", output, sha) == sha
    assert hashlib.sha256(output.read_bytes()).hexdigest() == sha
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        export_manifest(ledger, "pilot", output, sha)
    with runner.daemon.tracker.broker._connection as db:
        db.execute("UPDATE pilot_strategy_manifests SET payload=? WHERE sha256=?", ("{}", sha))
    with pytest.raises(ValueError, match="checksum"):
        export_manifest(ledger, "pilot", tmp_path / "bad.json", sha)
    assert not (tmp_path / "bad.json").exists()


@pytest.mark.parametrize(
    "field", ["stream", "fees", "model", "holiday", "special_session", "session", "specialist", "protection_tenant"]
)
def test_effective_strategy_fields_change_the_fingerprint(tmp_path, monkeypatch, field):
    from datetime import date, time

    from quant_ai.execution.session import SESSIONS, GlobalVenue

    runner, _ = setup(tmp_path, monkeypatch)
    d = runner.daemon
    before = d.strategy_manifest.check()["sha256"]
    if field == "stream":
        runner.streams[0].symbol_by_token[1] = "TCS"
    elif field == "fees":
        d.tracker.broker.friction_model.fixed_slippage_bps = Decimal(15)
    elif field == "model":
        d.scheduler.pipeline.runtime.cio.atlas.founder_instructions += " synthetic changed policy"
    elif field == "holiday":
        d.scheduler.calendar.holidays[GlobalVenue.INDIA] = frozenset({date(2026, 9, 15)})
    elif field == "special_session":
        d.scheduler.calendar.special_sessions[GlobalVenue.INDIA] = frozenset()
    elif field == "session":
        monkeypatch.setitem(
            SESSIONS, GlobalVenue.INDIA, replace(SESSIONS[GlobalVenue.INDIA], regular_open=time(10))
        )
    elif field == "protection_tenant":
        d.exit_engine.tenant_id = "different-tenant"
    else:
        monkeypatch.setattr(d.scheduler.pipeline.agents[0], "agent_id", "changed-specialist")
    result = d.strategy_manifest.check()
    assert result["sha256"] != before
    assert result["status"] == "changed"


def test_adaptive_observations_do_not_masquerade_as_policy_changes(tmp_path, monkeypatch):
    runner, _ = setup(tmp_path, monkeypatch)
    d = runner.daemon
    before = d.strategy_manifest.check()["sha256"]
    d.scheduler.pipeline.runtime.attribution.record(("technical-quant-mas",), Decimal(50))
    assert d.strategy_manifest.check()["sha256"] == before
    assert d.strategy_manifest.summary["status"] == "matched"


def test_registry_failure_is_unavailable_and_export_without_binding_is_explicit(
    tmp_path, monkeypatch
):
    runner, _ = setup(tmp_path, monkeypatch)
    broker = runner.daemon.tracker.broker
    now = datetime.now(timezone.utc)
    publish_tick(runner, "100", now)
    runner.daemon.protection_tick(now)
    with broker._connection as db:
        db.execute("UPDATE pilot_runtime SET payload=?", (json.dumps({"strategyManifest": None}),))
    with pytest.raises(ValueError, match="No recorded manifest"):
        export_manifest(tmp_path / "ledger.db", "pilot", tmp_path / "missing.json")
    broker._connection.execute("DROP TABLE pilot_strategy_manifests")
    assert runner.daemon.strategy_manifest.check()["status"] == "unavailable"
    runner.daemon.protection_tick(now)
    assert runner.daemon.kill_switch.engaged


def test_governed_fill_retains_the_checked_runtime_binding(tmp_path, monkeypatch):
    from quant_ai.agents.swarm import AgentAnalysisRequest, TradeProposal
    from quant_ai.domain.models import AssetClass, PortfolioSnapshot

    runner, _ = setup(tmp_path, monkeypatch)
    d = runner.daemon
    now = datetime(2026, 9, 15, 6, tzinfo=timezone.utc)
    d.clock = lambda: now
    publish_tick(runner, "100", now)
    proposal = TradeProposal(
        "synthetic-manifest-fill",
        "INFY",
        Market.INDIA,
        "INDIA",
        AssetClass.EQUITY,
        Side.BUY,
        1,
        Decimal(100),
        Decimal(95),
        Decimal(110),
        Decimal(".8"),
        Decimal(".02"),
        Decimal(".01"),
        ("Synthetic QA",),
    )
    result = d.scheduler.pipeline.runtime._execute_proposal(
        AgentAnalysisRequest("INFY", Market.INDIA, AssetClass.EQUITY, now, {}),
        (),
        proposal,
        d.plan,
        PortfolioSnapshot(Decimal(100000), Decimal(0), Decimal(0)),
        None,
        "pilot",
    )
    assert result.fill is not None, result.risk_decision.reason
    row = d.tracker.broker._connection.execute(
        "SELECT payload FROM paper_decision_evidence"
    ).fetchone()
    binding = json.loads(row[0])["provenance"]["runtime_strategy"]
    assert binding["sha256"] == d.strategy_manifest.summary["sha256"]
    assert binding["checkedAt"] == now.isoformat()
    assert binding["status"] == "matched"
    sha = binding["sha256"]
    recorded = d.tracker.broker._connection.execute(
        "SELECT payload FROM pilot_strategy_manifests WHERE tenant_id=? AND sha256=?",
        ("pilot", sha),
    ).fetchone()[0]
    assert hashlib.sha256(recorded.encode()).hexdigest() == sha


def test_sdk_endpoint_retry_timeout_and_custom_transport_are_not_silent(tmp_path, monkeypatch):
    import asyncio

    from quant_ai.llm.anthropic_client import AnthropicSwarmClient

    runner, _ = setup(tmp_path, monkeypatch)
    d = runner.daemon
    client = AnthropicSwarmClient(
        api_key="synthetic-credential-never-export", model="synthetic-model"
    )
    d.scheduler.pipeline.runtime.cio.atlas.llm_client = client
    try:
        first = d.strategy_manifest.capture()
        assert not first["issues"]
        assert "synthetic-credential" not in json.dumps(first)
        client._client.base_url = "https://different-provider.example.test/v1/"
        second = d.strategy_manifest.capture()
        assert second["sha256"] != first["sha256"]
        client._client.max_retries += 1
        third = d.strategy_manifest.capture()
        assert third["sha256"] != second["sha256"]
        client._client.timeout = 7.5
        assert d.strategy_manifest.capture()["sha256"] != third["sha256"]
    finally:
        asyncio.run(client._client.close())
    client._client = SimpleNamespace()
    client.transport_kind = "injected_client"
    assert "unsupported_inference_transport" in d.strategy_manifest.capture()["issues"]


def test_custom_protective_resolver_cannot_inherit_a_complete_binding(tmp_path, monkeypatch):
    runner, source = setup(tmp_path, monkeypatch)
    runner.daemon.exit_engine.mark_resolver = lambda position: Decimal(100)
    runner.daemon.bind_strategy_manifest(runner.streams, source_root=source, revision="a" * 40)
    summary = runner.daemon.strategy_manifest.summary
    assert summary["status"] == "incomplete"
    assert "unsupported_protective_mark_resolver" in summary["issues"]
