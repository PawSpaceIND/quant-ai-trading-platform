import json
import sqlite3
from decimal import Decimal

import pytest

from quant_ai.domain.models import Market, OrderIntent, Side
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.operations import recovery_bundle as bundle


def fixture(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    broker = PaperBrokerService(source / "ledger")
    fill = broker.buy(OrderIntent("INFY", Market.INDIA, Side.BUY, 2, Decimal(100), "synthetic", tenant_id="pilot"))
    with sqlite3.connect(source / "console") as db:
        db.executescript("CREATE TABLE preferences(id INTEGER); CREATE TABLE conversations(id INTEGER); CREATE TABLE audit(id INTEGER); INSERT INTO conversations VALUES(1);")
    for name in ("proofs", "reviews"):
        (source / name).mkdir()
    (source / "proofs" / "fill.json").write_text(json.dumps({"order_id": fill.order_id, "fixture": True}))
    (source / "directives").write_text('{"fixture":true}')
    (source / "halt").write_text("remain halted for recovery review")
    return {"revision": "a" * 40, "tenant": "pilot", "sources": {name: str(source / name) for name in bundle.KINDS}}


def test_bundle_restores_all_selected_state_without_activation(tmp_path):
    spec = fixture(tmp_path)
    manifest = bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    result = bundle.restore(tmp_path / "backup", tmp_path / "restored", manifest_sha256=manifest["manifestSha256"])
    assert result["status"] == "restored", result
    assert result["haltPresent"]
    assert result["consoleCounts"]["conversations"] == 1
    assert result["proofCoverage"]["missingCount"] == 0
    assert result["reconciliation"]["status"] == "matched"
    assert "research" in manifest["absent"]
    assert (tmp_path / "restored" / "halt").read_text() == "remain halted for recovery review"
    assert (tmp_path / "restored" / "console").stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        bundle.restore(tmp_path / "backup", tmp_path / "restored", manifest_sha256=manifest["manifestSha256"])


def test_file_and_manifest_tampering_are_detected_before_restore(tmp_path):
    spec = fixture(tmp_path)
    manifest = bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    (tmp_path / "backup" / "directives").write_text("tampered")
    with pytest.raises(ValueError, match="checksum"):
        bundle.restore(tmp_path / "backup", tmp_path / "restored", manifest_sha256=manifest["manifestSha256"])
    assert not (tmp_path / "restored").exists()
    data = json.loads((tmp_path / "backup" / "manifest.json").read_text())
    data["files"]["directives"] = {"sha256": bundle.digest(tmp_path / "backup" / "directives"), "size": 8}
    (tmp_path / "backup" / "manifest.json").write_text(json.dumps(data))
    with pytest.raises(ValueError, match="Trusted manifest"):
        bundle.restore(tmp_path / "backup", tmp_path / "restored", manifest_sha256=manifest["manifestSha256"])


def test_changing_source_refuses_bundle_and_cleans_only_new_output(tmp_path, monkeypatch):
    spec = fixture(tmp_path)
    original = bundle.sqlite_backup
    def change(source, target):
        original(source, target)
        (tmp_path / "source" / "directives").write_text("changed during capture")
    monkeypatch.setattr(bundle, "sqlite_backup", change)
    with pytest.raises(ValueError, match="Source changed"):
        bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    assert not (tmp_path / "backup").exists()
    assert (tmp_path / "source" / "directives").read_text() == "changed during capture"


def test_missing_proof_restores_but_reports_unclosed_recovery_gap(tmp_path):
    spec = fixture(tmp_path)
    (tmp_path / "source" / "proofs" / "fill.json").unlink()
    manifest = bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    result = bundle.restore(tmp_path / "backup", tmp_path / "restored", manifest_sha256=manifest["manifestSha256"])
    assert result["status"] == "discrepancy"
    assert result["proofCoverage"]["missingCount"] == 1
    assert (tmp_path / "restored" / "restore-report.json").exists()


def test_sources_require_stopped_writer_declaration_and_no_symlinks(tmp_path):
    spec = fixture(tmp_path)
    with pytest.raises(ValueError, match="Stop all writers"):
        bundle.create(spec, tmp_path / "backup", writers_stopped=False)
    (tmp_path / "source" / "proofs" / "link").symlink_to(tmp_path / "source" / "directives")
    with pytest.raises(ValueError, match="Symlink"):
        bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    assert not (tmp_path / "backup").exists()


@pytest.mark.parametrize("path", ["../outside", "/tmp/outside", "a/../b", "", "a//b"])
def test_manifest_paths_cannot_escape_destination(path):
    with pytest.raises(ValueError):
        bundle.safe_relative(path)


def test_recovery_counts_atomic_ledger_protection_evidence(tmp_path):
    from quant_ai.execution.protective_exits import ProtectiveExitEngine
    spec=fixture(tmp_path)
    broker=PaperBrokerService(spec['sources']['ledger'])
    # Add a stored threshold to both entry record and position in this isolated fixture.
    broker._connection.execute("UPDATE paper_ledger SET stop_price='95'")
    broker._connection.execute("UPDATE paper_positions SET stop_price='95'")
    broker._connection.commit()
    assert ProtectiveExitEngine(broker,lambda _:Decimal(90),tenant_id='pilot').evaluate()[0].filled
    manifest=bundle.create(spec,tmp_path/'backup',writers_stopped=True)
    result=bundle.restore(tmp_path/'backup',tmp_path/'restored',manifest_sha256=manifest['manifestSha256'])
    assert result['status']=='restored'
    assert result['proofCoverage']['filledOrders']==2
    assert result['proofCoverage']['ledgerProtectionRecords']==1
    assert result['proofCoverage']['missingCount']==0
