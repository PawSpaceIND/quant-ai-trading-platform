"""The backup service: a real online backup on a schedule, with bounded retention."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "scheduled_backup", Path(__file__).parents[1] / "scripts/scheduled_backup.py"
)
scheduled_backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scheduled_backup)


def live_ledger(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.executescript(
        "PRAGMA journal_mode=WAL; CREATE TABLE fill(id INTEGER); INSERT INTO fill VALUES (7);"
    )
    return connection


def test_one_cycle_copies_a_live_wal_ledger_and_verifies_it(tmp_path) -> None:
    source = tmp_path / "pramana.db"
    directory = tmp_path / "backups"
    connection = live_ledger(source)

    assert scheduled_backup.cycle(source, directory, keep=14) is True

    copies = sorted(directory.glob("pramana-*.db"))
    assert len(copies) == 1
    manifest = json.loads(Path(str(copies[0]) + ".manifest.json").read_text())
    assert manifest["integrity"] == "ok"
    assert manifest["sha256"] == hashlib.sha256(copies[0].read_bytes()).hexdigest()
    with sqlite3.connect(copies[0]) as restored:
        assert restored.execute("SELECT id FROM fill").fetchone()[0] == 7
    # The engine's own connection is untouched and still writable.
    connection.execute("INSERT INTO fill VALUES (8)")
    connection.commit()
    connection.close()


def test_retention_keeps_the_newest_backups_and_their_manifests(tmp_path) -> None:
    directory = tmp_path / "backups"
    directory.mkdir()
    names = [
        scheduled_backup.backup_name(datetime(2026, 9, day, 3, 0, tzinfo=timezone.utc))
        for day in range(1, 8)
    ]
    for name in names:
        (directory / name).write_bytes(b"copy")
        (directory / (name + scheduled_backup.MANIFEST_SUFFIX)).write_text("{}")

    removed = scheduled_backup.prune(directory, keep=3)

    assert [path.name for path in removed] == names[:4]
    assert sorted(path.name for path in directory.glob("pramana-*.db")) == names[4:]
    assert sorted(path.name for path in directory.glob("*.manifest.json")) == [
        name + scheduled_backup.MANIFEST_SUFFIX for name in names[4:]
    ]


def test_retention_keeps_everything_while_under_the_cap(tmp_path) -> None:
    directory = tmp_path / "backups"
    directory.mkdir()
    name = scheduled_backup.backup_name(datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc))
    (directory / name).write_bytes(b"copy")

    assert scheduled_backup.prune(directory, keep=14) == ()
    assert (directory / name).is_file()


def test_a_failed_cycle_is_reported_without_raising(tmp_path) -> None:
    missing = tmp_path / "absent.db"

    assert scheduled_backup.cycle(missing, tmp_path / "backups", keep=14) is False


def test_once_mode_exits_nonzero_when_the_backup_cannot_be_taken(tmp_path) -> None:
    code = scheduled_backup.main(
        [
            "--database",
            str(tmp_path / "absent.db"),
            "--directory",
            str(tmp_path / "backups"),
            "--once",
        ]
    )

    assert code == 1


def test_once_mode_takes_exactly_one_backup(tmp_path) -> None:
    source = tmp_path / "pramana.db"
    connection = live_ledger(source)
    directory = tmp_path / "backups"

    code = scheduled_backup.main(
        ["--database", str(source), "--directory", str(directory), "--keep", "2", "--once"]
    )
    connection.close()

    assert code == 0
    assert len(list(directory.glob("pramana-*.db"))) == 1


def test_backup_names_sort_chronologically() -> None:
    earlier = scheduled_backup.backup_name(datetime(2026, 9, 9, 23, 59, tzinfo=timezone.utc))
    later = scheduled_backup.backup_name(datetime(2026, 9, 10, 0, 0, tzinfo=timezone.utc))

    assert earlier < later
    assert earlier.endswith(".db") and "20260909T235900Z" in earlier
