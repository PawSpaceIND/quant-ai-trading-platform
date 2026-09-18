"""Stopped-writer synthetic audit bundles; never resume recovery or account execution."""
import sqlite3
from pathlib import Path

import pytest
from test_institutional_bridge_recovery import interrupted_fill
from test_institutional_operator_recovery import body, route, setup_api
from test_institutional_swarm_bridge import BridgeHarness

from quant_ai.operations import recovery_bundle as bundle


def fixture(root, monkeypatch, *, outcome="returned"):
    root.mkdir()
    h = BridgeHarness(root)
    operations = None
    try:
        pid = interrupted_fill(h, monkeypatch)
        client, headers, operations, state, _credential = setup_api(root, h)
        if outcome != "empty":
            if outcome == "requested":
                def unavailable(*args, **kwargs):
                    raise OSError("synthetic loss of outcome audit")
                monkeypatch.setattr(operations, "_finish", unavailable)
            elif outcome == "failed":
                def unavailable(*args, **kwargs):
                    raise ValueError("synthetic unavailable accounting")
                monkeypatch.setattr(h.runtime, "reconcile_program", unavailable)
            response = client.post(route(pid), headers=headers,
                json=body(h.programs.get(pid).context_sha256))
            assert response.status_code == {"returned": 200, "requested": 503, "failed": 409}[outcome]
        client.close()
        # Persist no authentication credentials. Only operator key IDs are in this audit.
        assert all(value.key_hash not in str(operations._records())
                   for value in state.keys._credentials.values())
    finally:
        if operations is not None:
            operations.close()
        h.close()
    with sqlite3.connect(root / "console") as db:
        db.executescript("CREATE TABLE preferences(id INTEGER); CREATE TABLE conversations(id INTEGER); CREATE TABLE audit(id INTEGER);")
    db.close()
    (root / "reviews").mkdir()
    (root / "directives").write_text('{"synthetic": true}')
    (root / "halt").write_text("retain operator halt")
    spec = {"revision": "a" * 40, "tenant": "tenant", "sources": {
        name: str(root / ("paper.sqlite" if name == "ledger" else name)) for name in bundle.KINDS},
        "oms": str(root / "oms.sqlite"),
        "institutional_state": {name: str(root / (name + ".sqlite")) for name in ("accounting", "programs")},
        "operator_audit": str(root / "operator-audit.sqlite")}
    return spec, pid


def test_selected_operator_audit_is_restored_with_history_and_no_activation(tmp_path, monkeypatch):
    spec, _pid = fixture(tmp_path / "source", monkeypatch)
    original = Path(spec["operator_audit"]).read_bytes()
    manifest = bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    assert manifest["schema"] == 6
    assert "operator-audit" in manifest["files"]
    report = bundle.restore(tmp_path / "backup", tmp_path / "restored", manifest_sha256=manifest["manifestSha256"])
    assert report["status"] == "restored"
    audit = report["operatorRecovery"]
    assert audit["status"] == "selected_history_verified"
    assert audit["events"] == 2 and audit["returnedRequests"] == 1
    assert audit["requestsWithoutOutcome"] == 0
    assert audit["activationAuthorized"] is False
    assert audit["recoveryReplayed"] is False
    assert report["haltPresent"] is True
    assert Path(spec["operator_audit"]).read_bytes() == original
    assert (tmp_path / "restored/operator-audit").is_file()


def test_unselected_audit_is_not_claimed_to_be_captured(tmp_path, monkeypatch):
    spec, _ = fixture(tmp_path / "source", monkeypatch)
    del spec["operator_audit"]
    manifest = bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    report = bundle.restore(tmp_path / "backup", tmp_path / "restored", manifest_sha256=manifest["manifestSha256"])
    assert manifest["schema"] == 4
    assert report["operatorRecovery"]["status"] == "not_selected"



@pytest.mark.parametrize("outcome,event_count", [("empty", 0), ("requested", 1), ("failed", 2)])
def test_incomplete_audit_history_stays_incomplete_without_replaying(tmp_path, monkeypatch, outcome, event_count):
    spec, _ = fixture(tmp_path / "source", monkeypatch, outcome=outcome)
    manifest = bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    report = bundle.restore(tmp_path / "backup", tmp_path / "restored", manifest_sha256=manifest["manifestSha256"])
    audit = report["operatorRecovery"]
    assert report["status"] == "discrepancy"
    assert audit["events"] == event_count
    assert audit["requestsWithoutOutcome"] == int(outcome == "requested")
    assert audit["failedRequests"] == int(outcome == "failed")
    assert audit["returnedRequests"] == 0
    with sqlite3.connect(tmp_path / "restored/operator-audit") as db:
        assert db.execute("SELECT count(*) FROM institutional_operator_events").fetchone()[0] == event_count
    db.close()


@pytest.mark.parametrize("value", [None, "", " ", ":memory:", " missing ", 1, {}, []])
def test_invalid_audit_selection_refuses_before_creating_output(tmp_path, monkeypatch, value):
    spec, _ = fixture(tmp_path / "source", monkeypatch)
    spec["operator_audit"] = value
    with pytest.raises(ValueError, match="Operator audit recovery"):
        bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    assert not (tmp_path / "backup").exists()


@pytest.mark.parametrize("defect", ["missing", "directory", "symlink", "hardlink", "overlap", "missing_oms", "missing_accounting"])
def test_missing_aliased_or_partial_source_inventory_is_not_guessed(tmp_path, monkeypatch, defect):
    import os
    spec, _ = fixture(tmp_path / "source", monkeypatch)
    target = Path(spec["operator_audit"])
    if defect == "missing":
        spec["operator_audit"] = str(tmp_path / "nonexistent")
    elif defect == "directory":
        spec["operator_audit"] = str(tmp_path)
    elif defect in {"symlink", "hardlink"}:
        link = tmp_path / "link"
        if defect == "symlink":
            link.symlink_to(target)
        else:
            os.link(target, link)
        spec["operator_audit"] = str(link)
    elif defect == "overlap":
        spec["operator_audit"] = spec["oms"]
    elif defect == "missing_oms":
        del spec["oms"]
    else:
        del spec["institutional_state"]
    with pytest.raises(ValueError):
        bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    assert not (tmp_path / "backup").exists()


def rewrite_events(path, alter):
    import hashlib
    import json
    with sqlite3.connect(path) as db:
        db.execute("DROP TRIGGER institutional_operator_events_update_blocked")
        previous = "GENESIS"
        for seq, raw in db.execute("SELECT sequence,payload FROM institutional_operator_events ORDER BY sequence").fetchall():
            record = json.loads(raw)
            alter(record)
            record["previous_sha256"] = previous
            raw = json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False)
            previous = hashlib.sha256(raw.encode()).hexdigest()
            db.execute("UPDATE institutional_operator_events SET payload=?,sha256=? WHERE sequence=?", (raw, previous, seq))
    db.close()


@pytest.mark.parametrize("defect", ["hash", "tenant", "context", "program", "receipt", "revision", "future", "result_flag", "selection", "event_type"])
def test_recomputed_audit_hash_is_not_sufficient_for_source_correspondence(tmp_path, monkeypatch, defect):
    spec, _ = fixture(tmp_path / "source", monkeypatch)
    path = Path(spec["operator_audit"])
    if defect in {"hash", "selection"}:
        with sqlite3.connect(path) as db:
            if defect == "hash":
                db.execute("DROP TRIGGER institutional_operator_events_update_blocked")
                db.execute("UPDATE institutional_operator_events SET sha256='bad' WHERE sequence=1")
            else:
                db.execute("DROP TRIGGER institutional_operator_meta_update_blocked")
                db.execute("UPDATE institutional_operator_meta SET selection=?", ("f" * 64,))
        db.close()
    else:
        def alter(r):
            if defect == "tenant":
                r["tenant"] = "other"
            elif defect == "context":
                r["context_sha256"] = "a" * 64
            elif defect == "program":
                r["program_id"] = "other"
                if r["result"]:
                    r["result"]["program_id"] = "other"
            elif defect == "future":
                r["recorded_at"] = "2999-01-01T00:00:00+00:00"
            elif defect == "event_type":
                r["status"] = "UNRECORDED"
            elif r["result"]:
                if defect == "receipt":
                    r["result"]["committed_order_ids"] = ["PAPER-DIFFERENT"]
                elif defect == "revision":
                    r["result"]["source_revision_sha256"] = "b" * 64
                elif defect == "result_flag":
                    r["result"]["execution_authorized"] = True
        rewrite_events(path, alter)
    original = path.read_bytes()
    with pytest.raises(ValueError):
        bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    assert path.read_bytes() == original
    assert not (tmp_path / "backup").exists()


@pytest.mark.parametrize("defect", ["old_schema", "missing_inventory", "missing_file", "path", "typed_verification", "source_selection"])
def test_trusted_digest_does_not_skip_semantic_restore_validation(tmp_path, monkeypatch, defect):
    import json
    spec, _ = fixture(tmp_path / "source", monkeypatch)
    bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    path = tmp_path / "backup/manifest.json"
    manifest = json.loads(path.read_text())
    if defect == "old_schema":
        manifest["schema"] = 4
    elif defect == "missing_inventory":
        del manifest["operatorState"]
    elif defect == "missing_file":
        del manifest["files"]["operator-audit"]
        (tmp_path / "backup/operator-audit").unlink()
    elif defect == "path":
        manifest["operatorState"]["path"] = "../outside"
    elif defect == "typed_verification":
        manifest["operatorState"]["verification"]["activationAuthorized"] = 0
    else:
        manifest["operatorState"]["sourceSelectionSha256"] = "d" * 64
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="Operator audit recovery"):
        bundle.restore(tmp_path / "backup", tmp_path / "restored", manifest_sha256=bundle.digest(path))
    assert not (tmp_path / "restored").exists()


def test_audit_and_ai_inventory_compose_without_disabling_other_verifiers(tmp_path, monkeypatch):
    from test_ai_recovery_bundle import fixture as ai_fixture
    spec, _ = fixture(tmp_path / "source", monkeypatch)
    (tmp_path / "ai-source").mkdir()
    ai, _ = ai_fixture(tmp_path / "ai-source")
    spec["ai_state"] = {**ai["ai_state"], "tenant": spec["tenant"]}
    manifest = bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    report = bundle.restore(tmp_path / "backup", tmp_path / "restored", manifest_sha256=manifest["manifestSha256"])
    assert report["status"] == "restored"
    assert report["aiRecovery"]["activationAuthorized"] is False
    assert report["operatorRecovery"]["returnedRequests"] == 1
    assert report["institutionalRecovery"]["programs"]["storedRequestAndPlanVerified"] is True


def test_no_runtime_or_authentication_is_constructed_during_capture_or_restore(tmp_path, monkeypatch):
    from quant_ai.agents.institutional_runtime import InstitutionalSwarmPaperTradingService
    from quant_ai.operations.institutional_operator import InstitutionalRecoveryOperations
    from quant_ai.security.api_keys import ApiKeyRegistry
    spec, _ = fixture(tmp_path / "source", monkeypatch)
    def forbidden(*args, **kwargs):
        pytest.fail("Offline verification cannot construct or invoke runtime recovery/credentials")
    monkeypatch.setattr(InstitutionalRecoveryOperations, "__init__", forbidden)
    monkeypatch.setattr(InstitutionalSwarmPaperTradingService, "reconcile_program", forbidden)
    monkeypatch.setattr(ApiKeyRegistry, "issue", forbidden)
    manifest = bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    report = bundle.restore(tmp_path / "backup", tmp_path / "restored", manifest_sha256=manifest["manifestSha256"])
    assert report["operatorRecovery"]["recoveryReplayed"] is False
    assert report["operatorRecovery"]["credentialStoreCaptured"] is False


def test_read_only_capture_and_restore_preserve_every_source_and_audit_row(tmp_path, monkeypatch):
    spec, _ = fixture(tmp_path / "source", monkeypatch)
    paths, kinds, _ = bundle.source_plan(spec)
    before = bundle.inventory(paths, kinds)
    manifest = bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    bundle.restore(tmp_path / "backup", tmp_path / "restored", manifest_sha256=manifest["manifestSha256"])
    assert bundle.inventory(paths, kinds) == before
    with sqlite3.connect(Path(spec["operator_audit"]).as_uri() + "?mode=ro", uri=True) as source, sqlite3.connect(tmp_path / "restored/operator-audit") as restored:
        assert list(source.iterdump()) == list(restored.iterdump())
    source.close(); restored.close()


def test_existing_restore_directory_is_never_overwritten(tmp_path, monkeypatch):
    spec, _ = fixture(tmp_path / "source", monkeypatch)
    manifest = bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    (tmp_path / "restored").mkdir()
    marker = tmp_path / "restored/keep.txt"
    marker.write_text("existing data")
    with pytest.raises(FileExistsError):
        bundle.restore(tmp_path / "backup", tmp_path / "restored", manifest_sha256=manifest["manifestSha256"])
    assert marker.read_text() == "existing data"


def test_live_writer_drift_discards_only_partial_new_bundle(tmp_path, monkeypatch):
    spec, _ = fixture(tmp_path / "source", monkeypatch)
    original = bundle.sqlite_backup
    audit = Path(spec["operator_audit"])
    def changed(source, destination):
        original(source, destination)
        if destination.name == "operator-audit":
            with sqlite3.connect(audit) as db:
                db.execute("CREATE TABLE writer_changed(id INTEGER)")
            db.close()
    monkeypatch.setattr(bundle, "sqlite_backup", changed)
    with pytest.raises(ValueError, match="Source changed"):
        bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    assert audit.exists()
    assert not (tmp_path / "backup").exists()



def test_wal_committed_audit_frames_are_included_without_opening_a_writer(tmp_path, monkeypatch):
    import hashlib
    import json
    spec, _ = fixture(tmp_path / "source", monkeypatch)
    audit = Path(spec["operator_audit"])
    db = sqlite3.connect(audit)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("DROP TRIGGER institutional_operator_events_update_blocked")
        previous = "GENESIS"
        for number, raw in db.execute("SELECT sequence,payload FROM institutional_operator_events ORDER BY sequence").fetchall():
            value = json.loads(raw)
            value["actor_key_id"] = "synthetic-recovery-operator"
            value["previous_sha256"] = previous
            raw = json.dumps(value, sort_keys=True, separators=(",", ":"))
            previous = hashlib.sha256(raw.encode()).hexdigest()
            db.execute("UPDATE institutional_operator_events SET payload=?,sha256=? WHERE sequence=?", (raw, previous, number))
        db.commit()
        assert Path(str(audit) + "-wal").stat().st_size > 0
        rows = db.execute("SELECT sequence,payload,sha256 FROM institutional_operator_events ORDER BY sequence").fetchall()
        manifest = bundle.create(spec, tmp_path / "backup", writers_stopped=True)
        report = bundle.restore(tmp_path / "backup", tmp_path / "restored", manifest_sha256=manifest["manifestSha256"])
        assert report["operatorRecovery"]["returnedRequests"] == 1
        with sqlite3.connect(tmp_path / "restored/operator-audit") as restored:
            assert restored.execute("SELECT sequence,payload,sha256 FROM institutional_operator_events ORDER BY sequence").fetchall() == rows
        restored.close()
    finally:
        db.close()


@pytest.mark.parametrize("limit", ["MAX_EVENTS", "MAX_EVENT_BYTES"])
def test_audit_reader_limits_remain_active_offline(tmp_path, monkeypatch, limit):
    from quant_ai.operations import institutional_operator
    spec, _ = fixture(tmp_path / "source", monkeypatch)
    monkeypatch.setattr(institutional_operator, limit, 0)
    with pytest.raises(ValueError, match="audit_unavailable"):
        bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    assert not (tmp_path / "backup").exists()


def test_an_outcome_cannot_claim_a_recovered_sequence_without_a_receipt(tmp_path, monkeypatch):
    spec, _ = fixture(tmp_path / "source", monkeypatch)
    rewrite_events(Path(spec["operator_audit"]), lambda r: r["result"].update(committed_order_ids=[])
                   if r["result"] else None)
    with pytest.raises(ValueError, match="recovered sequence lacks committed evidence"):
        bundle.create(spec, tmp_path / "backup", writers_stopped=True)
    assert not (tmp_path / "backup").exists()
