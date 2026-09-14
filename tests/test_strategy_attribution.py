import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from test_trade_evidence import account, fill

from quant_ai.domain.models import Market, OrderIntent, Side
from quant_ai.governance.runtime_manifest import digest, encoded, stable
from quant_ai.validation.strategy_attribution import select_strategy_evidence
from quant_ai.validation.trade_evidence import build_trade_evidence


def register(broker, variant="a"):
    files = {"synthetic.py": variant * 64}
    manifest = {
        "schema": "pramana.runtime_strategy.v1",
        "tenant_id": "pilot",
        "release_revision": "a" * 40,
        "execution_mode": "paper",
        "source": {"files": files, "sha256": digest(files)},
    }
    sha = digest(manifest)
    with broker._connection as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS pilot_strategy_manifests(tenant_id TEXT,sha256 TEXT,created_at TEXT,payload TEXT,PRIMARY KEY(tenant_id,sha256))"
        )
        db.execute(
            "INSERT OR IGNORE INTO pilot_strategy_manifests VALUES(?,?,?,?)",
            ("pilot", sha, datetime.now(timezone.utc).isoformat(), encoded(manifest)),
        )
    return sha, manifest


def governed(broker, config, side=Side.BUY, quantity=1, price=100, *, protective=False):
    sha, manifest = config
    now = datetime.now(timezone.utc)
    binding = {
        "status": "matched",
        "sha256": sha,
        "bootSha256": sha,
        "sourceSha256": manifest["source"]["sha256"],
        "releaseRevision": "a" * 40,
        "checkedAt": now.isoformat(),
        "issues": [],
        "sourceCheckAgeSeconds": 0,
    }
    order = OrderIntent(
        "INFY", Market.INDIA, side, quantity, D(price), "synthetic-attribution", tenant_id="pilot",
        stop_price=D(95) if side == Side.BUY else None
    )
    if protective:
        return broker.sell_protected(
            order,
            {
                "schema": "pramana.protective_exit.v1",
                "event_type": "protective_exit",
                "runtime_strategy": binding,
                "proposal": {"side": "SELL"},
            },
            None,
        )
    proof = {
        "schema": "pramana.swarm_fill.v1",
        "event_type": "swarm_fill",
        "approved_order": stable(asdict(order)),
        "provenance": {"runtime_strategy": binding},
    }
    return broker.submit_with_evidence(order, proof, "synthetic-" + now.isoformat())


def report(broker, sha):
    whole = build_trade_evidence(broker._connection, "pilot")
    return whole, select_strategy_evidence(whole, sha)


def test_complete_scaled_episode_requires_each_entry_and_exit_to_match(tmp_path):
    b = account(tmp_path)
    cfg = register(b)
    governed(b, cfg, quantity=2)
    governed(b, cfg, quantity=1, price=110)
    governed(b, cfg, Side.SELL, quantity=1, price=120)
    _, opened = report(b, cfg[0])
    assert opened["summary"]["completedTrades"] == 0 and opened["summary"]["openEpisodes"] == 1
    governed(b, cfg, Side.SELL, quantity=2, price=105, protective=True)
    whole, selected = report(b, cfg[0])
    assert selected["summary"]["completedTrades"] == 1
    assert D(selected["summary"]["netPnl"]) == 20
    assert selected["unresolvedEpisodes"] == 0 and selected["foreignOpenEpisodes"] == 0
    assert whole["episodes"][0]["attribution"]["status"] == "linked"
    assert len(selected["completedEpisodeOrderIds"][0]) == 4


def test_mixed_configuration_does_not_cherry_pick_profitable_exits(tmp_path):
    b = account(tmp_path)
    a = register(b)
    c = register(b, "b")
    governed(b, a)
    governed(b, c, Side.SELL, price=150)
    whole, first = report(b, a[0])
    second = select_strategy_evidence(whole, c[0])
    assert whole["summary"]["netPnl"] == "50"
    assert whole["episodes"][0]["attribution"]["status"] == "mixed"
    for selected in (first, second):
        assert selected["summary"]["completedTrades"] == 0
        assert selected["unresolvedEpisodes"] == 1


def test_manual_entry_cannot_be_claimed_by_a_bound_protective_exit(tmp_path):
    b = account(tmp_path)
    cfg = register(b)
    fill(b, Side.BUY, 1, 100)
    governed(b, cfg, Side.SELL, price=90, protective=True)
    whole, selected = report(b, cfg[0])
    assert whole["summary"]["netPnl"] == "-10"
    assert selected["summary"]["completedTrades"] == 0
    assert selected["unresolvedEpisodes"] == 1
    assert selected["unlinkedAccountCompletedTrades"] == 1
    assert selected["incompatibleSessionDates"]


@pytest.mark.parametrize(
    "mutation",
    [
        "fees",
        "price",
        "quantity",
        "time",
        "stale",
        "future",
        "tenant",
        "manifest",
        "source",
        "status",
        "duplicate",
    ],
)
def test_mismatched_or_stale_evidence_is_excluded_without_hiding_account_result(tmp_path, mutation):
    b = account(tmp_path)
    cfg = register(b)
    buy = governed(b, cfg)
    governed(b, cfg, Side.SELL, price=110)
    row = b._connection.execute(
        "SELECT payload FROM paper_decision_evidence WHERE order_id=?", (buy.order_id,)
    ).fetchone()[0]
    proof = json.loads(row)
    binding = proof["provenance"]["runtime_strategy"]
    if mutation == "fees":
        proof["fill"]["cash_fees"] = "5"
    elif mutation == "price":
        proof["fill"]["price"] = "999"
    elif mutation == "quantity":
        proof["fill"]["quantity"] = True
    elif mutation == "time":
        proof["filled_at"] = "2000-01-01T00:00:00Z"
    elif mutation == "stale":
        binding["checkedAt"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    elif mutation == "future":
        binding["checkedAt"] = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    elif mutation == "tenant":
        proof["tenant_id"] = "other"
    elif mutation == "manifest":
        b._connection.execute("UPDATE pilot_strategy_manifests SET payload='{}'")
    elif mutation == "source":
        binding["sourceSha256"] = "f" * 64
    elif mutation == "status":
        binding["status"] = "changed"
    elif mutation == "duplicate":
        b._connection.execute(
            "INSERT INTO paper_protection_evidence VALUES(?,?,?)", (buy.order_id, "pilot", row)
        )
    with b._connection as db:
        db.execute(
            "UPDATE paper_decision_evidence SET payload=? WHERE order_id=?",
            (json.dumps(proof), buy.order_id),
        )
    whole, selected = report(b, cfg[0])
    assert whole["summary"]["completedTrades"] == 1 and whole["summary"]["netPnl"] == "10"
    if mutation == "manifest":
        assert selected is None
    else:
        assert selected["summary"]["completedTrades"] == 0
        assert selected["unresolvedEpisodes"] == 1


def test_missing_proof_changes_the_evidence_hash(tmp_path):
    b = account(tmp_path)
    cfg = register(b)
    buy = governed(b, cfg)
    governed(b, cfg, Side.SELL, price=110)
    _, before = report(b, cfg[0])
    _, unchanged = report(b, cfg[0])
    assert before["evidenceSha256"] == unchanged["evidenceSha256"]
    with b._connection as db:
        db.execute("DELETE FROM paper_decision_evidence WHERE order_id=?", (buy.order_id,))
    _, after = report(b, cfg[0])
    assert after["evidenceSha256"] != before["evidenceSha256"]
    assert after["summary"]["completedTrades"] == 0
    assert after["unresolvedEpisodes"] == 1


def test_unrelated_open_positions_and_legacy_trades_are_visible_not_attributed(tmp_path):
    b = account(tmp_path)
    cfg = register(b)
    fill(b, Side.BUY, 1, 100)
    fill(b, Side.SELL, 1, 110)
    fill(b, Side.BUY, 1, 100)
    whole, selected = report(b, cfg[0])
    assert whole["summary"]["completedTrades"] == 1
    assert selected["summary"]["completedTrades"] == 0
    assert selected["foreignOpenEpisodes"] == 1
    assert selected["unlinkedAccountCompletedTrades"] == 1
    assert select_strategy_evidence(whole, "0" * 64) is None


def test_real_factory_governed_entry_and_protective_exit_share_exact_manifest(
    tmp_path, monkeypatch
):
    from test_pilot_closure import publish_tick, runner_for

    from quant_ai.agents.swarm import AgentAnalysisRequest, TradeProposal
    from quant_ai.domain.models import AssetClass, PortfolioSnapshot

    monkeypatch.setenv("PRAMANA_RELEASE_REVISION", "a" * 40)
    r = runner_for(tmp_path)
    d = r.daemon
    b = d.tracker.broker
    now = datetime(2026, 9, 15, 6, tzinfo=timezone.utc)
    d.clock = lambda: now
    b._execution_time = now
    publish_tick(r, "100", now)
    proposal = TradeProposal(
        "synthetic-attribution-factory",
        "INFY",
        Market.INDIA,
        "INDIA",
        AssetClass.EQUITY,
        Side.BUY,
        1,
        D(100),
        D(95),
        D(110),
        D(".8"),
        D(".02"),
        D(".01"),
        ("synthetic",),
    )
    result = d.scheduler.pipeline.runtime._execute_proposal(
        AgentAnalysisRequest("INFY", Market.INDIA, AssetClass.EQUITY, now, {}),
        (),
        proposal,
        d.plan,
        PortfolioSnapshot(D(100000), D(0), D(0)),
        None,
        "pilot",
    )
    assert result.fill
    now += timedelta(minutes=1)
    b._execution_time = now
    publish_tick(r, "90", now)
    d.protection_tick(now)
    _, selected = report(b, d.strategy_manifest.summary["sha256"])
    assert selected["summary"]["completedTrades"] == 1
    assert D(selected["summary"]["netPnl"]) < 0
    telemetry = json.loads(b._connection.execute("SELECT payload FROM pilot_runtime").fetchone()[0])
    assert telemetry["strategyEvidence"]["evidenceSha256"] == selected["evidenceSha256"]
    assert not b.get_positions("pilot")


def test_protective_metadata_failure_does_not_prevent_liquidation(tmp_path):
    from quant_ai.execution.protective_exits import ProtectiveExitEngine

    b = account(tmp_path)
    cfg = register(b)
    # Synthetic direct seed with an actual stop; entry intentionally unproven.
    b.buy(
        OrderIntent(
            "INFY", Market.INDIA, Side.BUY, 1, D(100), "test", tenant_id="pilot", stop_price=D(95)
        )
    )
    engine = ProtectiveExitEngine(b, lambda position: D(90), tenant_id="pilot")

    def fail(now):
        raise RuntimeError("synthetic metadata failure")

    engine.strategy_manifest_provider = fail
    assert engine.evaluate()[0].filled
    assert not b.get_positions("pilot")
    evidence = json.loads(
        b._connection.execute("SELECT payload FROM paper_protection_evidence").fetchone()[0]
    )
    assert evidence["runtime_strategy"]["status"] == "unavailable"
    assert report(b, cfg[0])[1]["summary"]["completedTrades"] == 0


def test_entirely_unproven_episode_cannot_hide_a_loss_after_configuration_start(tmp_path):
    b = account(tmp_path)
    fill(b, Side.BUY, 1, 100)
    fill(b, Side.SELL, 1, 90)  # Before the configuration was first recorded.
    cfg = register(b)
    governed(b, cfg)
    governed(b, cfg, Side.SELL, price=110)
    _, before = report(b, cfg[0])
    assert before["unresolvedEpisodes"] == 0
    fill(b, Side.BUY, 1, 100)
    fill(b, Side.SELL, 1, 50)
    whole, after = report(b, cfg[0])
    assert after["summary"]["completedTrades"] == 1  # The linked statistic remains inspectable.
    assert after["unresolvedEpisodes"] == 1  # Acceptance cannot ignore the unproven loss.
    assert D(whole["summary"]["netPnl"]) == -50
    assert after["evidenceSha256"] != before["evidenceSha256"]


def test_after_the_fact_manifest_registration_cannot_qualify_an_earlier_fill(tmp_path):
    b = account(tmp_path)
    cfg = register(b)
    governed(b, cfg)
    governed(b, cfg, Side.SELL, price=110)
    with b._connection as db:
        db.execute(
            "UPDATE pilot_strategy_manifests SET created_at=?",
            ((datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),),
        )
    _, selected = report(b, cfg[0])
    assert selected["summary"]["completedTrades"] == 0
    assert selected["unresolvedEpisodes"] == 1


def test_corrected_recorded_fee_changes_metrics_and_invalidates_original_fill_proof(tmp_path):
    b = account(tmp_path)
    cfg = register(b)
    buy = governed(b, cfg)
    governed(b, cfg, Side.SELL, price=110)
    _, before = report(b, cfg[0])
    cash = b.get_margin("pilot").cash_balance
    with b._connection as db:
        db.execute(
            "INSERT INTO paper_cost_ledger(order_id,tenant_id,code,amount,cash_debit,created_at) VALUES(?,?,?,?,?,?)",
            (
                buy.order_id,
                "pilot",
                "SYNTHETIC_CORRECTION",
                "20",
                1,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        db.execute(
            "UPDATE paper_accounts SET cash_balance=? WHERE tenant_id='pilot'", (str(cash - D(20)),)
        )
    whole, after = report(b, cfg[0])
    assert whole["summary"]["netPnl"] == "-10"
    assert after["summary"]["completedTrades"] == 0 and after["unresolvedEpisodes"] == 1
    assert after["evidenceSha256"] != before["evidenceSha256"]
