"""Persisted local accounting selection, not broker ownership or operator approval.

The execution programme retains its selected accounting-store UUID and path.
Offline copies may ignore paths, never store identity. No account or trade is made.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from uuid import uuid4

SCHEMA = "pramana.institutional_accounting_binding.v1"
ERROR = "institutional_accounting_binding_mismatch"
MAX_BYTES = 8192
_ID = re.compile(r"[0-9a-f]{32}")
_CURRENCY = re.compile(r"[A-Z]{3}")


class AccountingBindingError(ValueError):
    pass


def _check(ok):
    if not ok:
        raise AccountingBindingError(ERROR)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        _check(key not in result)
        result[key] = value
    return result


def validate_binding(raw, tenant_id):
    """Validate stored structure without creating or opening another database."""
    if raw is None:
        return None
    try:
        _check(type(raw) is str and len(raw.encode()) <= MAX_BYTES)
        value = json.loads(raw, object_pairs_hook=_unique)
        _check(type(value) is dict and set(value) == {
            "schema", "tenant", "journalId", "baseCurrency", "storagePath"})
        _check(all(type(v) is str for v in value.values()) and _canonical(value) == raw)
        _check(value["schema"] == SCHEMA and value["tenant"] == tenant_id
               and bool(tenant_id) and tenant_id == tenant_id.strip()
               and _ID.fullmatch(value["journalId"]) and _CURRENCY.fullmatch(value["baseCurrency"]))
        path = value["storagePath"]
        _check(path == ":memory:" or bool(path) and Path(path).is_absolute()
               and str(Path(path)) == path and ".." not in Path(path).parts)
        return value
    except (TypeError, ValueError, KeyError, UnicodeError, RecursionError) as error:
        raise AccountingBindingError(ERROR) from error


def _read_identity(db):
    rows = db.execute("SELECT id,version,base_currency,store_identity FROM trading_journal_meta").fetchall()
    _check(len(rows) == 1)
    row = rows[0]
    _check(type(row[0]) is int and row[0] == 1 and type(row[1]) is int and row[1] == 1
           and type(row[2]) is str and _CURRENCY.fullmatch(row[2])
           and type(row[3]) is str and _ID.fullmatch(row[3]))
    return row[2], row[3]


def _storage_path(db):
    rows = [r for r in db.execute("PRAGMA database_list") if r[1] == "main"]
    _check(len(rows) == 1)
    path = rows[0][2]
    if not path:
        return ":memory:"
    file = Path(path)
    _check(file.is_absolute() and not file.is_symlink() and file.is_file()
           and file.stat().st_nlink == 1)
    return str(file.resolve())


def select_binding(journal, tenant_id):
    """Provision local metadata for a newly bound programme, never historical approval.

    Metadata commits before programme insertion. An unused identity after an
    interrupted first preparation is not a reservation, fill or execution permit.
    """
    _check(type(tenant_id) is str and bool(tenant_id) and tenant_id == tenant_id.strip())
    with journal.atomic():
        columns = {r[1] for r in journal.db.execute("PRAGMA table_info(trading_journal_meta)")}
        if "store_identity" not in columns:
            journal.db.execute("ALTER TABLE trading_journal_meta ADD COLUMN store_identity TEXT")
            rows = journal.db.execute("SELECT id,version,base_currency FROM trading_journal_meta").fetchall()
            _check(len(rows) == 1 and tuple(rows[0]) == (1, 1, journal.base_currency))
            journal.db.execute("UPDATE trading_journal_meta SET store_identity=? WHERE id=1", (uuid4().hex,))
        base, identity = _read_identity(journal.db)
        _check(base == journal.base_currency)
        path = _storage_path(journal.db)
        journal.db.execute("""CREATE TRIGGER IF NOT EXISTS trading_journal_store_identity_immutable
            BEFORE UPDATE OF store_identity ON trading_journal_meta
            BEGIN SELECT RAISE(ABORT,'Accounting store identity is immutable'); END""")
        journal.db.execute("""CREATE TRIGGER IF NOT EXISTS trading_journal_store_identity_retained
            BEFORE DELETE ON trading_journal_meta
            BEGIN SELECT RAISE(ABORT,'Accounting store identity is retained'); END""")
    raw = _canonical({"schema": SCHEMA, "tenant": tenant_id, "journalId": identity,
                      "baseCurrency": base, "storagePath": path})
    validate_binding(raw, tenant_id)
    return raw


def verify_connection(raw, db, tenant_id, *, check_paths=True):
    """Read-only check of the supplied connection; missing identity is never repaired."""
    value = validate_binding(raw, tenant_id)
    if value is None:
        return False
    try:
        base, identity = _read_identity(db)
        _check((base, identity) == (value["baseCurrency"], value["journalId"]))
        if check_paths:
            _check(_storage_path(db) == value["storagePath"])
    except (sqlite3.Error, OSError, ValueError, TypeError) as error:
        raise AccountingBindingError(ERROR) from error
    return True


def verify_binding(raw, journal, tenant_id):
    with journal._lock:
        return verify_connection(raw, journal.db, tenant_id)
