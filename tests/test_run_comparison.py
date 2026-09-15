import json
import sqlite3
from contextlib import closing
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

import pytest
from run_comparison_fixture import END, START, add_mature_history, dataset, fixture

from quant_ai.backtesting.replay import HistoricalReplayHarness
from quant_ai.execution.audit import XAITraceLogger
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.governance.runtime_manifest import digest, encoded
from quant_ai.validation.run_comparison import build, capture


def test_windowed_capture_survives_thirty_days_and_retains_pre_window_fills(paired):
    folder, reports = paired
    replay = capture(folder / "replay.sqlite", "replay", "replay", reports["runId"])
    start = (START + timedelta(minutes=5)).isoformat()
    original = build(
        capture(folder / "paper.sqlite", "default", "paper"), replay, start, END.isoformat()
    )
    add_mature_history(folder / "paper.sqlite")
    with closing(sqlite3.connect(folder / "paper.sqlite")) as db:
        # Unrelated old manifests do not overwhelm the requested observation window.
        db.execute(
            "CREATE TABLE IF NOT EXISTS pilot_strategy_manifests(tenant_id TEXT,sha256 TEXT,payload TEXT)"
        )
        db.executemany(
            "INSERT INTO pilot_strategy_manifests(tenant_id,sha256,payload) VALUES ('default',?,?)",
            [(f"{i:064x}", "old unselected payload") for i in range(101)],
        )
        db.commit()
    with pytest.raises(ValueError, match="paper_observations_exceed_capture_bounds"):
        capture(folder / "paper.sqlite", "default", "paper")
    paper = capture(folder / "paper.sqlite", "default", "paper", start=start, end=END.isoformat())
    assert len(paper["rows"]) == 64
    assert len(paper["fills"]) == 2 and paper["fills"][0]["created_at"] < start
    assert paper["manifests"] == []
    selected = paper["observationSelection"]
    assert selected["totalAccountObservations"] == 43269
    assert selected["excludedObservations"] == 43205
    current = build(paper, replay, start, END.isoformat())
    assert current["curve"] == original["curve"]
    assert current["fills"] == original["fills"]
    assert current["initialState"] == "different_recorded_book"
    assert current["curve"][0]["paper"]["holdings"][0]["quantity"] == 8
    with pytest.raises(ValueError, match="paper_capture_window_mismatch"):
        build(paper, replay, START.isoformat(), END.isoformat())


def test_windowed_capture_resolves_offsets_without_accepting_duplicate_or_unknown_buckets(paired):
    folder, reports = paired
    database = folder / "paper.sqlite"
    replay = capture(folder / "replay.sqlite", "replay", "replay", reports["runId"])
    with closing(sqlite3.connect(database)) as db:
        db.execute(
            "UPDATE paper_live_valuations SET timestamp=? WHERE timestamp=?",
            ("2026-09-11T09:30:00+05:30", START.isoformat()),
        )
        db.commit()
        paper = capture(database, "default", "paper", start=START.isoformat(), end=END.isoformat())
        assert len(paper["rows"]) == 69
        assert (
            build(paper, replay, START.isoformat(), END.isoformat())["curve"]
            == reports["gapped"]["curve"]
        )
        db.execute(
            "INSERT INTO paper_live_valuations SELECT tenant_id,?,ledger_id,payload FROM paper_live_valuations WHERE timestamp=?",
            (START.isoformat(), "2026-09-11T09:30:00+05:30"),
        )
        db.commit()
        paper = capture(database, "default", "paper", start=START.isoformat(), end=END.isoformat())
        with pytest.raises(ValueError, match="unordered_valuation_clock"):
            build(paper, replay, START.isoformat(), END.isoformat())
        db.execute(
            "UPDATE paper_live_valuations SET timestamp='unknown' WHERE timestamp=?",
            (START.isoformat(),),
        )
        db.commit()
        with pytest.raises(ValueError, match="unlocatable_paper_observation"):
            capture(database, "default", "paper", start=START.isoformat(), end=END.isoformat())


def test_windowed_capture_keeps_selected_size_bounds_and_current_account_reconciliation(paired):
    folder, _ = paired
    database = folder / "paper.sqlite"
    with closing(sqlite3.connect(database)) as db:
        db.execute(
            "UPDATE paper_live_valuations SET payload=? WHERE timestamp=?",
            ("x" * 16_000_001, START.isoformat()),
        )
        db.commit()
        with pytest.raises(ValueError, match="paper_observations_exceed_capture_bounds"):
            capture(database, "default", "paper", start=START.isoformat(), end=END.isoformat())
        db.execute("UPDATE paper_accounts SET cash_balance='1' WHERE tenant_id='default'")
        db.commit()
        with pytest.raises(ValueError, match="source_account_not_reconciled"):
            capture(database, "default", "paper", start=START.isoformat(), end=END.isoformat())


@pytest.fixture
def paired(tmp_path):
    return tmp_path, fixture(tmp_path)


def test_actual_harness_and_telemetry_reconcile_known_sizes_fees_and_divergence(paired):
    _, reports = paired
    report = reports["complete"]
    assert report["status"] == "complete_observations"
    assert report["initialState"] == "same_recorded_book"
    assert len(report["curve"]) == 70
    # Independent cash-flow oracle: separate source fills, no broker-reported P&L.
    values = {}
    for mode in ("paper", "replay"):
        fills = report["fills"][mode]
        assert [f["quantity"] for f in fills] == ([8, 8] if mode == "paper" else [10, 10])
        cash = Decimal(100000)
        for f in fills:
            # Fees and notional are applied as two cash movements, exactly as a ledger
            # applies them: one Decimal expression would re-associate the sum and round
            # the last place of a 28-digit total differently from the book it audits.
            cash -= Decimal(f["cashFees"])
            cash += (1 if f["side"] == "SELL" else -1) * f["quantity"] * Decimal(f["price"])
        values[mode] = cash
        assert Decimal(report["curve"][-1][mode]["equity"]) == cash
    assert (
        Decimal(report["metrics"]["endingEquityDifference"]) == values["paper"] - values["replay"]
    )
    assert [g["quantityDifference"] for g in report["fillGroups"]] == [-2, -2]
    assert report["configuration"]["strategyEquivalence"] == "unverified"
    assert report["automaticPromotion"] is False


def test_missing_invalid_and_skewed_minutes_are_not_interpolated_or_counted_as_complete(paired):
    folder, reports = paired
    r = reports["gapped"]
    assert r["metrics"] is None and r["status"] == "incomplete_observations"
    assert [(i, p["issues"]) for i, p in enumerate(r["curve"]) if p["issues"]] == [
        (20, ["paper_valuation_invalid"]),
        (30, ["paper_observation_missing"]),
        (40, ["observation_time_mismatch"]),
    ]
    assert all(r["curve"][i]["equityDifference"] is None for i in (20, 30, 40))
    p = capture(folder / "paper.sqlite", "default", "paper")
    replay = capture(folder / "replay.sqlite", "replay", "replay", reports["runId"])
    tolerated = build(p, replay, START.isoformat(), END.isoformat(), max_skew_seconds=15)
    assert tolerated["curve"][40]["issues"] == []
    assert tolerated["metrics"] is None, (
        "A chosen skew tolerance does not repair absent or invalid observations"
    )


def test_account_mismatch_future_fill_or_invalid_cash_cannot_be_reported_as_a_valid_point(paired):
    folder, reports = paired
    p = capture(folder / "paper.sqlite", "default", "paper")
    r = capture(folder / "replay.sqlite", "replay", "replay", reports["runId"])
    bad = deepcopy(p)
    value = json.loads(bad["rows"][10]["payload"])
    value["cash"] += 100
    bad["rows"][10]["payload"] = json.dumps(value)
    report = build(bad, r, START.isoformat(), END.isoformat())
    assert report["curve"][10]["paper"]["equity"] is None
    bad = deepcopy(p)
    value = json.loads(bad["rows"][0]["payload"])
    value["tenantId"] = "other"
    bad["rows"][0]["payload"] = json.dumps(value)
    with pytest.raises(ValueError, match="identity"):
        build(bad, r, START.isoformat(), END.isoformat())
    bad = deepcopy(p)
    bad["fills"][0]["created_at"] = (START + timedelta(days=10)).isoformat()
    with pytest.raises(ValueError):
        build(bad, r, START.isoformat(), END.isoformat())


def test_retained_run_survives_later_backdated_replay_and_legacy_projection_replacement(paired):
    folder, reports = paired
    original = capture(folder / "replay.sqlite", "replay", "replay", reports["runId"])
    with closing(PaperBrokerService(folder / "replay.sqlite")) as broker:
        from quant_ai.domain.models import RiskMode
        from quant_ai.planning.capital import CapitalGoalEngine, CapitalPlanRequest

        plan = CapitalGoalEngine().recommend(
            CapitalPlanRequest(
                Decimal(100000),
                Decimal(".8"),
                Decimal(".2"),
                expected_edge=Decimal(".02"),
                requested_mode=RiskMode.BALANCED,
            )
        )
        second = HistoricalReplayHarness(
            broker,
            plan,
            quantity=10,
            country="India",
            tenant_id="replay",
            xai_logger=XAITraceLogger(folder / "later-proofs"),
        ).run(dataset())
        assert second.replay_run_id != reports["runId"]
        assert (
            broker._connection.execute("SELECT count(*) FROM paper_replay_runs").fetchone()[0] == 2
        )
    assert capture(folder / "replay.sqlite", "replay", "replay", reports["runId"]) == original


@pytest.mark.parametrize("target", ["points", "fees", "metadata", "status"])
def test_changed_or_unfinished_run_is_rejected(paired, target):
    folder, reports = paired
    with closing(sqlite3.connect(folder / "replay.sqlite")) as db:
        if target == "points":
            db.execute("DELETE FROM paper_replay_run_points WHERE sequence=3")
        if target == "fees":
            db.execute(
                "UPDATE paper_cost_ledger SET amount='0' WHERE id=(SELECT min(id) FROM paper_cost_ledger)"
            )
        if target == "metadata":
            db.execute("UPDATE paper_replay_runs SET metadata='{}'")
        if target == "status":
            db.execute("UPDATE paper_replay_runs SET status='running'")
        db.commit()
    with pytest.raises((ValueError, KeyError)):
        capture(folder / "replay.sqlite", "replay", "replay", reports["runId"])


def test_replay_recording_rejects_wrong_tenant_without_changing_latest_projection(paired):
    folder, _ = paired
    with closing(PaperBrokerService(folder / "replay.sqlite")) as broker:
        before = [
            tuple(r) for r in broker._connection.execute("SELECT * FROM paper_replay_valuations")
        ]
        with pytest.raises(ValueError, match="not_active"):
            broker.record_replay_valuation(START.isoformat(), "{}", "replay", "f" * 32)
        assert [
            tuple(r) for r in broker._connection.execute("SELECT * FROM paper_replay_valuations")
        ] == before


def test_calendar_window_and_starting_state_cannot_be_silently_rebased_to_a_better_subset(paired):
    folder, reports = paired
    p = capture(folder / "paper.sqlite", "default", "paper")
    r = capture(folder / "replay.sqlite", "replay", "replay", reports["runId"])
    for start, end in [
        (START.isoformat(), START.isoformat()),
        ((START + timedelta(seconds=1)).isoformat(), END.isoformat()),
        ("2026-09-14T04:00:00+00:00", "2026-09-14T05:00:00+00:00"),
    ]:
        with pytest.raises(ValueError):
            build(p, r, start, end)
    report = build(p, r, (START - timedelta(minutes=1)).isoformat(), END.isoformat())
    assert report["curve"][0]["issues"] == [
        "paper_observation_missing",
        "replay_observation_missing",
    ]
    assert report["initialState"] == "unavailable" and report["metrics"] is None


def test_source_configuration_comparison_reports_actual_component_differences(paired):
    folder, reports = paired
    p = capture(folder / "paper.sqlite", "default", "paper")
    r = capture(folder / "replay.sqlite", "replay", "replay", reports["runId"])
    components = deepcopy(r["replay"]["configuration"]["components"])
    components["llm"] = {"type": "a.different.model"}
    components["daemon"] = {
        "parameters": {k: r["replay"]["configuration"][k] for k in ("plan", "quantity", "country")}
    }
    manifest = {
        "tenant_id": "default",
        "source": r["replay"]["source"],
        "components": components,
        "news_window_seconds": r["replay"]["configuration"]["newsWindowSeconds"],
    }
    sha = digest(manifest)
    p["manifests"] = [{"sha256": sha, "payload": encoded(manifest)}]
    for row in p["rows"]:
        v = json.loads(row["payload"])
        v["strategyObservation"] = {"manifestSha256": sha, "eligible": True}
        row["payload"] = json.dumps(v)
    result = build(p, r, START.isoformat(), END.isoformat())["configuration"]
    assert result["sourceComparison"] == "same_inventory" and result["differentComponents"] == [
        "llm"
    ]
    assert result["strategyEquivalence"] == "unverified"


def test_failed_harness_keeps_incomplete_run_but_cannot_publish_comparison(tmp_path, monkeypatch):
    from quant_ai.domain.models import RiskMode
    from quant_ai.planning.capital import CapitalGoalEngine, CapitalPlanRequest

    plan = CapitalGoalEngine().recommend(
        CapitalPlanRequest(
            Decimal(100000),
            Decimal(".8"),
            Decimal(".2"),
            expected_edge=Decimal(".02"),
            requested_mode=RiskMode.BALANCED,
        )
    )
    original = HistoricalReplayHarness._record_valuation

    def fail(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("synthetic interruption")

    monkeypatch.setattr(HistoricalReplayHarness, "_record_valuation", fail)
    with closing(PaperBrokerService(tmp_path / "replay.sqlite")) as broker:
        with pytest.raises(RuntimeError, match="synthetic interruption"):
            HistoricalReplayHarness(
                broker,
                plan,
                quantity=10,
                country="India",
                tenant_id="replay",
                xai_logger=XAITraceLogger(tmp_path / "proofs"),
            ).run(dataset())
        row = broker._connection.execute("SELECT run_id,status FROM paper_replay_runs").fetchone()
        assert row["status"] == "failed"
        assert (
            broker._connection.execute("SELECT count(*) FROM paper_replay_run_points").fetchone()[0]
            == 1
        )
    with pytest.raises(ValueError, match="completed_replay"):
        capture(tmp_path / "replay.sqlite", "replay", "replay", row["run_id"])
    from quant_ai.operations.research_recovery import inspect

    assert (
        inspect(tmp_path / "replay.sqlite", "replay_ledger")["runs"][row["run_id"]]["status"]
        == "failed"
    )


def test_replay_wal_backup_and_restored_comparison_preserve_run_identity_and_gaps(paired):
    from pathlib import Path

    from test_recovery_bundle import fixture as paper_fixture

    from quant_ai.operations import recovery_bundle as bundle
    from quant_ai.operations.research_recovery import inspect

    folder, reports = paired
    (folder / "pilot").mkdir()
    spec = paper_fixture(folder / "pilot")
    spec["research_state"] = {
        "replay": {"kind": "replay_ledger", "path": str(folder / "replay.sqlite")},
        "paired-report": {"kind": "file", "path": str(folder / "gapped.json")},
    }
    with closing(sqlite3.connect(folder / "replay.sqlite")) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("UPDATE paper_replay_runs SET created_at=created_at||' '")
        writer.commit()
        assert Path(str(folder / "replay.sqlite") + "-wal").stat().st_size > 0
        manifest = bundle.create(spec, folder / "backup", writers_stopped=True)
        result = bundle.restore(
            folder / "backup", folder / "restored", manifest_sha256=manifest["manifestSha256"]
        )
    assert result["researchRecovery"]["status"] == "selected_state_verified"
    restored = folder / "restored/research-state/replay"
    assert inspect(restored, "replay_ledger") == manifest["researchState"]["replay"]["verification"]
    assert capture(restored, "replay", "replay", reports["runId"]) == capture(
        folder / "replay.sqlite", "replay", "replay", reports["runId"]
    )
    assert (folder / "restored/research-state/paired-report").read_bytes() == (
        folder / "gapped.json"
    ).read_bytes()
    with sqlite3.connect(restored) as db:
        db.execute("UPDATE paper_replay_run_points SET payload='{}' WHERE sequence=1")
    with pytest.raises(ValueError, match="hash changed"):
        inspect(restored, "replay_ledger")


def test_an_unfinished_run_blocks_another_harness_on_the_same_replay_account(paired):
    from quant_ai.domain.models import RiskMode
    from quant_ai.planning.capital import CapitalGoalEngine, CapitalPlanRequest

    folder, reports = paired
    original = capture(folder / "replay.sqlite", "replay", "replay", reports["runId"])
    with sqlite3.connect(folder / "replay.sqlite") as db:
        db.execute(
            "INSERT INTO paper_replay_runs SELECT ?,tenant_id,'running',metadata,NULL,created_at FROM paper_replay_runs WHERE run_id=?",
            ("f" * 32, reports["runId"]),
        )
    plan = CapitalGoalEngine().recommend(
        CapitalPlanRequest(
            Decimal(100000),
            Decimal(".8"),
            Decimal(".2"),
            expected_edge=Decimal(".02"),
            requested_mode=RiskMode.BALANCED,
        )
    )
    with closing(PaperBrokerService(folder / "replay.sqlite")) as broker:
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            HistoricalReplayHarness(
                broker,
                plan,
                quantity=10,
                country="India",
                tenant_id="replay",
                xai_logger=XAITraceLogger(folder / "blocked-proofs"),
            ).run(dataset())
        assert (
            broker._connection.execute("SELECT count(*) FROM paper_replay_runs").fetchone()[0] == 2
        )
    assert capture(folder / "replay.sqlite", "replay", "replay", reports["runId"]) == original
