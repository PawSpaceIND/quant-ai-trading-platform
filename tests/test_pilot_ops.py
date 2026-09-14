import importlib.util
import sqlite3
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("pilot_ops", Path(__file__).parents[1] / "scripts/pilot_ops.py")
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)


def test_consistent_backup_includes_wal_and_refuses_overwrite(tmp_path):
    source = tmp_path / "source.sqlite"
    destination = tmp_path / "backup.sqlite"
    connection = sqlite3.connect(source)
    connection.executescript("PRAGMA journal_mode=WAL; CREATE TABLE proof(id INTEGER); INSERT INTO proof VALUES (42);")
    result = ops.backup(source, destination)
    with sqlite3.connect(destination) as restored:
        assert restored.execute("SELECT id FROM proof").fetchone()[0] == 42
    assert result["integrity"] == "ok"
    assert len(result["sha256"]) == 64
    with pytest.raises(FileExistsError):
        ops.backup(source, destination)
    connection.close()


def test_health_needs_fresh_engine_heartbeat(tmp_path):
    database = tmp_path / "engine.sqlite"
    with sqlite3.connect(database) as db:
        db.executescript("CREATE TABLE pilot_runtime(tenant_id TEXT,updated_at TEXT,payload TEXT); INSERT INTO pilot_runtime VALUES ('pilot','2000-01-01T00:00:00+00:00','{}');")
    result = ops.health(database, "pilot")
    assert result["status"] == "unhealthy"
    assert "stored_heartbeat_stale_or_invalid" in result["reasons"]
