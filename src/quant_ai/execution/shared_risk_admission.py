"""Broker-owned witnesses for reserved, possibly never-executed paper programmes.

Witnesses never authorize orders or release capacity. A journal rollback can lose
pending reservations without a fill to expose that loss; these independent records
retain the original reservation/parent/schedule identity. All-store rollback and
privileged rewriting of both sources remain outside this local integrity check.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime

from quant_ai.execution.risk_authority import validate_authority
from quant_ai.execution.shared_risk import SharedRiskError
from quant_ai.orders.intent import order_from_snapshot

TABLE = "paper_shared_risk_admission_witnesses"
SCHEMA = "pramana.shared_risk_admission_witness.v1"
MAX_RECORDS = 100_000
MAX_SLICES = 10_000


def _check(condition, reason):
    if not condition:
        raise SharedRiskError("shared_risk_broker_admission_witness_" + reason)


def _raw(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def _rows(db, query, args=(), *, limit=None):
    limit = MAX_RECORDS if limit is None else limit
    rows = db.execute(query, args).fetchmany(limit + 1)
    _check(len(rows) <= limit, "inventory_limit")
    return rows


def _moment(raw):
    _check(isinstance(raw, str), "timestamp_invalid")
    at = datetime.fromisoformat(raw)
    _check(at.utcoffset() is not None, "timestamp_invalid")
    return at


def create_schema(ledger):
    """Only explicit new-account binding may create this optional table."""
    ledger.execute(f"CREATE TABLE {TABLE}(tenant_id TEXT NOT NULL,program_id TEXT NOT NULL,"
                   "payload TEXT NOT NULL,sha256 TEXT NOT NULL,PRIMARY KEY(tenant_id,program_id))")
    for action in ("UPDATE", "DELETE"):
        ledger.execute(f"CREATE TRIGGER shared_risk_admission_{action.lower()}_blocked BEFORE {action} "
            f"ON {TABLE} BEGIN SELECT RAISE(ABORT,'Shared risk admission witness is immutable'); END")


def _expected(journal, pin, program_id):
    tenant = pin["tenantId"]
    program = journal.execute("SELECT * FROM execution_programs WHERE tenant_id=? AND program_id=?",
                              (tenant, program_id)).fetchone()
    charge = journal.execute("SELECT * FROM shared_risk_reservations WHERE tenant_id=? AND program_id=?",
                             (tenant, program_id)).fetchone()
    _check(program is not None and charge is not None, "program_missing")
    _check(charge["sha256"] == _sha(charge["payload"]), "reservation_hash_invalid")
    try:
        authority = validate_authority(program["risk_authority_version"], program["risk_authority_payload"],
            program_id=program_id, tenant_id=tenant, parent_payload=program["parent_order_payload"],
            runtime_digest=program["runtime_context_sha256"])
        if authority is None or authority["mode"] != "ENTRY":
            raise ValueError("entry_authority_required")
    except (TypeError, ValueError, KeyError, IndexError) as error:
        # Preserve the existing final broker's authority error contract.
        raise SharedRiskError("shared_risk_broker_program_authority_invalid") from error
    parent = order_from_snapshot(program["parent_order_payload"])
    _check(type(program["parent_quantity"]) is int and program["parent_quantity"] == parent.quantity
           and program["symbol"] == parent.symbol, "parent_mismatch")
    created = _moment(program["created_at"])
    rows = _rows(journal, "SELECT sequence,quantity,scheduled_at FROM execution_program_slices "
                         "WHERE program_id=? ORDER BY sequence", (program_id,), limit=MAX_SLICES)
    _check(bool(rows), "schedule_missing")
    schedule, quantity, previous = [], 0, created
    for index, row in enumerate(rows, 1):
        _check(type(row["sequence"]) is int and row["sequence"] == index
               and type(row["quantity"]) is int and 0 < row["quantity"] <= parent.quantity,
               "schedule_invalid")
        at = _moment(row["scheduled_at"])
        _check(at >= previous, "schedule_time_invalid")
        previous = at
        quantity += row["quantity"]
        schedule.append({"sequence": index, "quantity": row["quantity"], "scheduledAt": row["scheduled_at"]})
    _check(quantity == parent.quantity, "schedule_quantity_mismatch")
    return _raw({"schema": SCHEMA, "tenantId": tenant, "programId": program_id,
        "journalId": pin["journalId"], "brokerBindingSha256": _sha(_raw(pin)),
        "reservationSha256": charge["sha256"], "authoritySha256": program["runtime_context_sha256"],
        "parentSha256": _sha(program["parent_order_payload"]), "planSha256": program["plan_sha256"],
        "decisionId": program["decision_id"], "createdAt": program["created_at"],
        "scheduleSha256": _sha(_raw(schedule))})


def verify_admissions(ledger, journal, pin, *, pending_program_id=None):
    """Compare both inventories; normal readers never allow an unwitnessed programme.

    The private append path may temporarily permit the one new programme in the
    still-uncommitted journal transaction. It cannot excuse a missing older witness.
    """
    try:
        exists = ledger.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone()
        _check(exists is not None, "table_missing")
        columns = {r[1] for r in ledger.execute(f"PRAGMA table_info({TABLE})")}
        _check(columns == {"tenant_id", "program_id", "payload", "sha256"}, "schema_invalid")
        tenant = pin["tenantId"]
        charges = _rows(journal, "SELECT program_id FROM shared_risk_reservations WHERE tenant_id=?", (tenant,))
        expected_ids = {r[0] for r in charges}
        rows = _rows(ledger, f"SELECT program_id,payload,sha256 FROM {TABLE} WHERE tenant_id=?", (tenant,))
        recorded_ids = {r[0] for r in rows}
        _check(len(expected_ids) == len(charges) and len(recorded_ids) == len(rows), "duplicate_identity")
        missing = expected_ids - recorded_ids
        _check(not recorded_ids - expected_ids, "journal_history_missing")
        _check(not missing or (journal.in_transaction and pending_program_id is not None
               and missing == {pending_program_id}), "record_missing")
        _check(bool(expected_ids), "empty_history")
        for row in rows:
            expected = _expected(journal, pin, row[0])
            _check(row[1] == expected and row[2] == _sha(expected), "history_mismatch")
        return len(rows)
    except SharedRiskError:
        raise
    except (ValueError, TypeError, KeyError, IndexError, ArithmeticError, sqlite3.Error, RecursionError) as error:
        raise SharedRiskError("shared_risk_broker_admission_witness_invalid") from error


def record_admission(ledger, journal, pin, program_id):
    """Append while journal creation is uncommitted; caller commits broker first.

    A later journal failure leaves an independent witness and therefore holds new
    entries. No retry fabricates its missing programme or erases the witness.
    """
    _check(journal.in_transaction and isinstance(program_id, str) and bool(program_id),
           "pending_transaction_required")
    verify_admissions(ledger, journal, pin, pending_program_id=program_id)
    raw = _expected(journal, pin, program_id)
    existing = ledger.execute(f"SELECT payload,sha256 FROM {TABLE} WHERE tenant_id=? AND program_id=?",
                              (pin["tenantId"], program_id)).fetchone()
    if existing is None:
        state = journal.execute("SELECT state FROM execution_programs WHERE program_id=?", (program_id,)).fetchone()
        claimed = journal.execute("SELECT 1 FROM execution_program_slices WHERE program_id=? AND "
            "(state!='PENDING' OR client_order_id IS NOT NULL OR broker_order_id IS NOT NULL) LIMIT 1",
            (program_id,)).fetchone()
        _check(state is not None and state[0] == "PLANNED" and claimed is None, "cannot_backfill_claimed_program")
        ledger.execute(f"INSERT INTO {TABLE} VALUES(?,?,?,?)", (pin["tenantId"], program_id, raw, _sha(raw)))
    else:
        _check(tuple(existing) == (raw, _sha(raw)), "repeated_identity_changed")
    verify_admissions(ledger, journal, pin)
