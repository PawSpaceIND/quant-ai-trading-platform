import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from test_recovery_bundle import fixture as paper_fixture
from test_research_extensions import at, config, feed, lab_fixture, order, quote

from quant_ai.operations import recovery_bundle as bundle
from quant_ai.operations.research_recovery import inspect
from quant_ai.research.company_events import CompanyEvents
from quant_ai.research.lab import ResearchLab, canonical
from quant_ai.research.portfolio_sim import PortfolioJournal
from quant_ai.research.portfolio_workspace import publish as publish_portfolio
from quant_ai.research.providers import Provider, evaluate_case
from quant_ai.research.workspace_report import publish as publish_comparison


def research_fixture(tmp_path):
    spec = paper_fixture(tmp_path)
    source = tmp_path / "source"
    with closing(lab_fixture(source)) as lab:
        evidence = lab.export_evidence("exp")
    with closing(PortfolioJournal(source / "portfolio.sqlite", config())) as journal:
        for event in [quote(0), order(1), quote(2), order(3, side="SELL"),
                      quote(4, price="110", qty=2), {"id": "gap", "kind": "clock", "at": at(40)}]:
            journal.append(event)
    with closing(CompanyEvents(source / "events.sqlite")) as events:
        events.ingest(feed(), observed_at=at(10))
        events.map_company("Infosys Limited", "NSE:INFY", at(11), "synthetic mapping review")
    publish_comparison(source / "research.db", "exp", "pilot", source / "comparison.json")
    publish_portfolio(source / "portfolio.sqlite", "synthetic replay", "pilot", source / "portfolio.json")
    receipts = source / "receipts"
    receipts.mkdir()
    input_hash = evidence["body"]["cases"][0]["input_digest"]
    receipt_id = hashlib.sha256(canonical(["exp", "case", "claude", input_hash]).encode()).hexdigest()
    (receipts / f"{receipt_id}.json").write_text(json.dumps({
        "state": "pending", "experiment": "exp", "case_id": "case",
        "candidate": "claude", "input_digest": input_hash,
    }))
    spec["research_state"] = {
        name: {"kind": kind, "path": str(source / path)}
        for name, kind, path in [
            ("experiments", "experiment_journal", "research.db"),
            ("portfolios", "portfolio_journal", "portfolio.sqlite"),
            ("events", "company_events", "events.sqlite"),
            ("receipts", "directory", "receipts"),
            ("comparison-report", "file", "comparison.json"),
            ("portfolio-report", "file", "portfolio.json"),
        ]
    }
    return spec


def test_full_research_recovery_preserves_replay_sources_reports_and_pending_receipts(tmp_path, monkeypatch):
    spec = research_fixture(tmp_path)
    manifest = bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    result = bundle.restore(tmp_path / "backup", tmp_path / "restored", manifest_sha256=manifest["manifestSha256"])
    assert result["status"] == "restored"
    research = result["researchRecovery"]
    assert research["status"] == "selected_state_verified"
    assert set(research["sources"]) == set(spec["research_state"])
    for name, original in spec["research_state"].items():
        path = tmp_path / "restored" / research["sources"][name]["path"]
        assert inspect(path, original["kind"]) == manifest["researchState"][name]["verification"]
        assert path.stat().st_mode & 0o777 == (0o700 if path.is_dir() else 0o600)
        if original["kind"] == "file":
            assert path.read_bytes() == Path(original["path"]).read_bytes()
    folder = tmp_path / "restored" / "research-state"
    with closing(PortfolioJournal(folder / "portfolios", readonly=True)) as journal:
        book = journal.report()["books"]["claude"]
        assert book["cash_inr"] == "720" and book["realized_pnl_inr"] == "20"
        assert book["positions"]["NSE:INFY"]["quantity"] == 3
        assert book["curve"][-1]["equity_inr"] is None
    # Preserve raw XML bytes and time-aware mappings, not just JSON reports.
    for table in ("feed_captures", "event_revisions", "symbol_mappings"):
        with closing(sqlite3.connect(spec["research_state"]["events"]["path"])) as a, closing(sqlite3.connect(folder / "events")) as b:
            assert a.execute(f"SELECT * FROM {table}").fetchall() == b.execute(f"SELECT * FROM {table}").fetchall()
    def unexpected_request(*args):
        pytest.fail("Recovery must retain the paid-request guard")
    monkeypatch.setattr(Provider, "request", unexpected_request)
    with closing(ResearchLab(folder / "experiments")) as lab, pytest.raises(FileExistsError):
        evaluate_case(lab, "exp", "case", {
            "claude": Provider("anthropic", "synthetic"),
            "astra": Provider("openai", "synthetic"),
        }, folder / "receipts")
    assert json.loads(next((folder / "receipts").glob("*.json")).read_text())["state"] == "pending"


def test_research_wal_commits_are_captured_in_standalone_database(tmp_path):
    spec = research_fixture(tmp_path)
    path = Path(spec["research_state"]["portfolios"]["path"])
    with closing(PortfolioJournal(path, config())) as writer:
        writer.db.execute("PRAGMA journal_mode=WAL")
        writer.db.execute("PRAGMA wal_autocheckpoint=0")
        writer.append(quote(41, price="90"))
        assert Path(str(path) + "-wal").stat().st_size > 0
        expected = writer.export_evidence()["sha256"]
        manifest = bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    result = bundle.restore(tmp_path / "backup", tmp_path / "restored", manifest_sha256=manifest["manifestSha256"])
    assert result["researchRecovery"]["sources"]["portfolios"]["evidenceSha256"] == expected
    assert not list((tmp_path / "backup").rglob("*-wal"))


def test_changed_research_source_aborts_capture_without_overwriting_source(tmp_path, monkeypatch):
    spec = research_fixture(tmp_path)
    original = bundle.sqlite_backup
    def change(source, target):
        original(source, target)
        if target.name == "portfolios":
            with closing(PortfolioJournal(source, config())) as writer:
                writer.append(quote(41))
    monkeypatch.setattr(bundle, "sqlite_backup", change)
    with pytest.raises(ValueError, match="Source changed"):
        bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    assert not (tmp_path / "backup").exists()
    with closing(PortfolioJournal(spec["research_state"]["portfolios"]["path"], readonly=True)) as journal:
        assert len(journal.events()) == 7


@pytest.mark.parametrize("mutation", ["missing", "overlap", "traversal", "unknown_kind", "typo", "symlink"])
def test_invalid_or_omitted_research_selection_cannot_silently_pass(tmp_path, mutation):
    spec = research_fixture(tmp_path)
    if mutation == "missing":
        spec["research_state"]["events"]["path"] += ".missing"
    elif mutation == "overlap":
        spec["research_state"]["events"]["path"] = spec["sources"]["ledger"]
    elif mutation == "traversal":
        spec["research_state"]["../escape"] = spec["research_state"].pop("events")
    elif mutation == "unknown_kind":
        spec["research_state"]["events"]["kind"] = "auto"
    elif mutation == "typo":
        spec["researchState"] = spec.pop("research_state")
    else:
        link = tmp_path / "link"
        link.symlink_to(spec["research_state"]["events"]["path"])
        spec["research_state"]["events"]["path"] = str(link)
    with pytest.raises(ValueError):
        bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    assert not (tmp_path / "backup").exists()


def test_wrong_database_kind_and_orphan_research_rows_are_rejected(tmp_path):
    spec = research_fixture(tmp_path)
    spec["research_state"]["events"]["kind"] = "portfolio_journal"
    with pytest.raises(ValueError, match="kind/schema"):
        bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    spec["research_state"]["events"]["kind"] = "company_events"
    with sqlite3.connect(spec["research_state"]["experiments"]["path"]) as db:
        db.execute("INSERT INTO outcomes VALUES('exp','missing','{}')")
    with pytest.raises(ValueError, match="foreign-key"):
        bundle.create(spec, tmp_path / "backup", writers_stopped=True)


def test_replay_drift_fails_restore_and_cleans_only_new_destination(tmp_path, monkeypatch):
    spec = research_fixture(tmp_path)
    manifest = bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    original = PortfolioJournal.report
    def changed(self):
        value = original(self)
        value["books"]["claude"]["cash_inr"] = "999"
        return value
    monkeypatch.setattr(PortfolioJournal, "report", changed)
    with pytest.raises(ValueError, match="verification mismatch"):
        bundle.restore(tmp_path / "backup", tmp_path / "restored", manifest_sha256=manifest["manifestSha256"])
    assert not (tmp_path / "restored").exists()
    assert (tmp_path / "backup" / "manifest.json").exists()


@pytest.mark.parametrize("damage", ["changed_capture", "missing_receipt", "invalid_event"])
def test_research_corruption_is_not_a_successful_backup_or_restore(tmp_path, damage):
    spec = research_fixture(tmp_path)
    if damage == "invalid_event":
        with sqlite3.connect(spec["research_state"]["portfolios"]["path"]) as db:
            db.execute("UPDATE simulation_events SET seq=99 WHERE seq=0")
        with pytest.raises(ValueError, match="sequence_or_identity"):
            bundle.create(spec, tmp_path / "backup", writers_stopped=True)
        assert not (tmp_path / "backup").exists()
        return
    manifest = bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    research = tmp_path / "backup" / "research-state"
    if damage == "changed_capture":
        with sqlite3.connect(research / "events") as db:
            db.execute("UPDATE feed_captures SET raw=?", (b"changed raw evidence",))
    else:
        next((research / "receipts").glob("*.json")).unlink()
    with pytest.raises(ValueError, match="checksum|inventory"):
        bundle.restore(tmp_path / "backup", tmp_path / "restored", manifest_sha256=manifest["manifestSha256"])
    assert not (tmp_path / "restored").exists()


def test_legacy_bundle_remains_restorable_without_implying_research_coverage(tmp_path):
    spec = paper_fixture(tmp_path)
    bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    path = tmp_path / "backup" / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["schema"] = 1
    del manifest["researchState"]
    path.write_text(json.dumps(manifest))
    result = bundle.restore(tmp_path / "backup", tmp_path / "restored", manifest_sha256=bundle.digest(path))
    assert result["status"] == "restored"
    assert result["researchRecovery"]["status"] == "not_selected"


def test_restored_withdrawal_does_not_resurrect_company_mapping(tmp_path):
    spec = research_fixture(tmp_path)
    with closing(CompanyEvents(spec["research_state"]["events"]["path"])) as events:
        record = events.revoke_company("Infosys Limited", "withdrawn before backup", at(11))
        expected = events.db.execute("SELECT * FROM symbol_mappings ORDER BY verified_at").fetchall()
    manifest = bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    result = bundle.restore(tmp_path / "backup", tmp_path / "restored", manifest_sha256=manifest["manifestSha256"])
    assert result["researchRecovery"]["status"] == "selected_state_verified"
    with closing(CompanyEvents(tmp_path / "restored" / "research-state" / "events", readonly=True)) as events:
        assert events.db.execute("SELECT * FROM symbol_mappings ORDER BY verified_at").fetchall() == expected
        assert events.sources_as_of("NSE:INFY", record["verified_at"]) == []
        assert len(events.sources_as_of("NSE:INFY", at(12))) == 1
