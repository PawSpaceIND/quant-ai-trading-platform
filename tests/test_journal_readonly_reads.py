"""A reader newer than the ledger it is pointed at must read it, not try to migrate it.

``scripts/pilot_ops.py`` opens the live book ``mode=ro`` on purpose, so an operator shell
cannot write to it. ``load_rows`` calls ``ensure_journal``, which ALTERs in any column the
ledger predates - and on a read-only connection that is
``attempt to write a readonly database``.

The window is real and badly placed. Between a deploy that adds a column and the session's
first journalled decision, the table is one migration behind and every read of it fails:
``missed``, ``plan``, the premarket check. That is the pre-open hour, which is exactly when
an operator reads.

Two rules fix it and both are pinned here. The migration is best-effort for a reader and
still loud for a writer, and a row from a ledger that predates a column carries None for
it - never a zero, because those decisions genuinely have nothing to put there.
"""
from __future__ import annotations

import re
import sqlite3
from threading import RLock
from types import SimpleNamespace

import pytest

from quant_ai.analytics import decision_journal as journal

REQUIRED = re.compile(r"^\s+(\w+) (?:TEXT|INTEGER) NOT NULL", re.MULTILINE)


def _old_schema(*, without: tuple[str, ...]) -> str:
    """The CREATE TABLE this build would have written before ``without`` existed."""
    schema = journal.SCHEMA
    for column in without:
        schema = schema.replace(f"    {column} TEXT,\n", "")
    return schema


def _ledger(tmp_path, *, without: tuple[str, ...], rows: int = 1):
    schema = _old_schema(without=without)
    path = tmp_path / "old.sqlite"
    connection = sqlite3.connect(str(path))
    connection.execute(schema)
    for index in range(rows):
        values = {name: "x" for name in REQUIRED.findall(schema)}
        values.update(decision_id=f"d{index}", tenant_id="ghost", symbol="INFY",
                      decided_at=f"2026-09-22T05:0{index}:00+00:00")
        connection.execute(
            f"INSERT INTO {journal.TABLE} ({', '.join(values)}) "
            f"VALUES ({', '.join('?' * len(values))})", tuple(values.values()))
    connection.commit()
    connection.close()
    return path


class ReadOnly:
    """What ``pilot_ops`` hands the journal: the two attributes, over mode=ro."""

    def __init__(self, path) -> None:
        self._lock = RLock()
        self._connection = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=1)
        self._connection.row_factory = sqlite3.Row


class Writable:
    def __init__(self, path) -> None:
        self._lock = RLock()
        self._connection = sqlite3.connect(str(path))
        self._connection.row_factory = sqlite3.Row


def test_a_read_only_ledger_one_migration_behind_is_still_readable(tmp_path) -> None:
    """The failure this closes, on the exact columns the host is behind on today."""
    path = _ledger(tmp_path, without=journal.FILL_COLUMNS)
    rows = journal.load_rows(ReadOnly(path), tenant_id="ghost")
    assert len(rows) == 1 and rows[0]["symbol"] == "INFY"


def test_a_column_the_ledger_predates_reads_as_absent_and_not_as_zero(tmp_path) -> None:
    """Every caller still gets a full row; the missing ones are honestly empty.

    A zero would be a value - a drift of zero basis points, a spread of zero - and a
    reader could not tell it from a measurement that was never taken.
    """
    path = _ledger(tmp_path, without=journal.FILL_COLUMNS)
    row = journal.load_rows(ReadOnly(path), tenant_id="ghost")[0]
    assert set(journal.COLUMNS) <= set(row)
    assert all(row[column] is None for column in journal.FILL_COLUMNS)


def test_a_writer_still_migrates_the_ledger(tmp_path) -> None:
    """Best-effort for a reader must not become best-effort for the daemon."""
    path = _ledger(tmp_path, without=journal.FILL_COLUMNS)
    broker = Writable(path)
    assert not set(journal.FILL_COLUMNS) <= journal.journal_columns(broker)
    journal.ensure_journal(broker)
    assert set(journal.FILL_COLUMNS) <= journal.journal_columns(broker)


class Malformed:
    """A connection whose every statement fails for a reason that is not read-only."""

    def __enter__(self):
        return self

    def __exit__(self, *_exception) -> bool:
        return False

    def execute(self, *_args, **_kwargs):
        raise sqlite3.OperationalError("database disk image is malformed")


def test_only_the_read_only_cause_is_tolerated() -> None:
    """A corrupt table or a locked writer is a failure, not a reader being ahead.

    Swallowing every OperationalError would turn the next genuine schema fault into a
    journal that silently stopped growing - the one failure this table exists to prevent.
    """
    broker = SimpleNamespace(_lock=RLock(), _connection=Malformed())
    with pytest.raises(sqlite3.OperationalError, match="malformed"):
        journal.ensure_journal(broker)


def test_the_reader_is_not_passing_for_free(tmp_path) -> None:
    """A ledger that is NOT behind must still return the columns it has.

    Without this the tolerant read could be hiding a SELECT that drops every new column
    on every ledger, and the two tests above would not notice.
    """
    path = _ledger(tmp_path, without=())
    broker = Writable(path)
    journal.ensure_journal(broker)
    with broker._lock:
        broker._connection.execute(
            f"UPDATE {journal.TABLE} SET drift_bps = '15.0', mark_at_submit = '100.15'")
        broker._connection.commit()
    row = journal.load_rows(ReadOnly(path), tenant_id="ghost")[0]
    assert (row["drift_bps"], row["mark_at_submit"]) == ("15.0", "100.15")
