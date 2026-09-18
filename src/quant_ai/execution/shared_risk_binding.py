"""Local broker-ledger pin for an explicitly selected shared-risk journal.

This adds refusal checks, not a new order transport, account limit, capacity release
or broker-account authentication. Independent covered exits never read the journal.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from quant_ai.domain.models import AssetClass, Market, Side
from quant_ai.execution.reconciliation import reconcile_paper
from quant_ai.execution.risk_authority import validate_authority
from quant_ai.execution.shared_risk import (
    SharedRiskError,
    _decoded_amount,
    _json,
    _rows,
    verify_shared_risk,
)
from quant_ai.execution.shared_risk_admission import TABLE as ADMISSION_TABLE
from quant_ai.execution.shared_risk_admission import (
    create_schema,
    record_admission,
    verify_admissions,
)
from quant_ai.orders.intent import bound_identity, canonical_order_intent, order_from_snapshot
from quant_ai.orders.oms import DurableOms

TABLE = "paper_shared_risk_bindings"
COLUMN = "shared_risk_binding_sha256"
LEGACY_SCHEMA = "pramana.paper_shared_risk_binding.v1"
SCHEMA = "pramana.paper_shared_risk_binding.v2"
SHA = re.compile(r"[0-9a-f]{64}")
JID = re.compile(r"[0-9a-f]{32}")
MAX_COMMITTED_ENTRIES = 100_000


def _check(condition, reason):
    if not condition:
        raise SharedRiskError("shared_risk_broker_" + reason)


def _raw(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def database_path(db):
    path = next((r[2] for r in db.execute("PRAGMA database_list") if r[1] == "main"), "")
    _check(bool(path), "durable_storage_required")
    candidate = Path(path)
    _check(candidate.is_file() and not candidate.is_symlink()
           and candidate.stat().st_nlink == 1, "unaliased_file_required")
    return str(candidate.resolve())


def ledger_key(ledger_path, journal_path, tenant):
    return _sha(json.dumps([ledger_path, journal_path, tenant]))


def read_binding(ledger, tenant):
    """A missing row cannot erase the account's independent immutable witness."""
    columns = {r[1] for r in ledger.execute("PRAGMA table_info(paper_accounts)")}
    account = (ledger.execute(f"SELECT {COLUMN} FROM paper_accounts WHERE tenant_id=?",
               (tenant,)).fetchone() if COLUMN in columns else None)
    witness = None if account is None else account[0]
    exists = ledger.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone()
    row = ledger.execute(f"SELECT payload,sha256 FROM {TABLE} WHERE tenant_id=?", (tenant,)).fetchone() if exists else None
    if row is None and witness is None:
        return None
    _check(row is not None and isinstance(witness, str) and SHA.fullmatch(witness), "binding_missing")
    value = _json(row[0])
    _check(isinstance(value, dict) and set(value) == {"schema", "tenantId", "accountRef",
        "currency", "journalId", "journalPath", "ledgerPath", "ledgerKey", "policySha256"},
        "binding_fields_invalid")
    _check(value["schema"] in {LEGACY_SCHEMA, SCHEMA} and value["tenantId"] == tenant
           and isinstance(value["journalId"], str) and JID.fullmatch(value["journalId"])
           and value["currency"] in {"INR", "USD"}
           and row[1] == witness == _sha(row[0]), "binding_integrity_mismatch")
    for name in ("ledgerPath", "journalPath"):
        path = value[name]
        _check(isinstance(path, str) and Path(path).is_absolute() and str(Path(path)) == path,
               "binding_path_invalid")
    _check(value["ledgerPath"] != value["journalPath"]
           and value["ledgerKey"] == ledger_key(value["ledgerPath"], value["journalPath"], tenant),
           "binding_path_invalid")
    return value


def verify_binding_pair(ledger, journal, tenant, *, check_paths=True):
    """Offline restore can compare identities without silently rebinding saved paths."""
    return _verify_binding_pair(ledger, journal, tenant, check_paths=check_paths)


def _verify_binding_pair(ledger, journal, tenant, *, check_paths=True, pending_program_id=None):
    pin = read_binding(ledger, tenant)
    try:
        report = verify_shared_risk(journal, tenant)
    except (ValueError, KeyError, TypeError, sqlite3.Error) as error:
        raise SharedRiskError("shared_risk_broker_journal_invalid") from error
    if pin is None and report["status"] == "not_selected":
        return None
    _check(pin is not None and report["status"] == "consistent", "journal_binding_missing")
    policy = report["policy"]
    _check(pin["journalId"] == report.get("journalId")
           and pin["policySha256"] == report["policySha256"]
           and pin["ledgerKey"] == policy["ledgerKey"]
           and pin["accountRef"] == policy["accountRef"]
           and pin["currency"] == policy["currency"], "journal_identity_mismatch")
    if check_paths:
        _check(database_path(ledger) == pin["ledgerPath"]
               and database_path(journal) == pin["journalPath"], "journal_path_mismatch")
    verify_committed_entry_coverage(ledger, journal, tenant, pin)
    if pin["schema"] == LEGACY_SCHEMA:
        _check(not check_paths, "admission_witness_migration_required")
    else:
        verify_admissions(ledger, journal, pin, pending_program_id=pending_program_id)
    return pin


def _source_payload(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _check(key not in result, "committed_entry_duplicate_field")
            result[key] = value
        return result
    def nonfinite(_value):
        raise SharedRiskError("shared_risk_broker_committed_entry_nonfinite_json")
    _check(isinstance(raw, str) and len(raw.encode()) <= 1_000_000,
           "committed_entry_receipt_missing")
    return json.loads(raw, object_pairs_hook=unique, parse_constant=nonfinite)


def verify_committed_entry_coverage(ledger, journal, tenant, pin):
    """A matching UUID does not prove that a restored journal covers committed buys.

    Compare existing broker-owned entry receipts with retained parent/slice claims.
    DISPATCHING may already have a committed broker receipt, so recovery remains
    possible without inventing an OMS or accounting completion. Nothing is posted,
    freed or reconstructed here. Unexecuted-history rollback is a separate boundary.
    """
    try:
        rows = ledger.execute("""SELECT l.*,e.payload AS source_payload FROM paper_ledger l
            LEFT JOIN paper_decision_evidence e ON e.order_id=l.order_id AND e.tenant_id=l.tenant_id
            WHERE l.tenant_id=? AND l.side='BUY' ORDER BY l.id""", (tenant,)).fetchmany(MAX_COMMITTED_ENTRIES + 1)
        _check(len(rows) <= MAX_COMMITTED_ENTRIES, "committed_entry_inventory_limit")
        seen_orders, seen_slices = set(), set()
        for entry in rows:
            payload = _source_payload(entry["source_payload"])
            _check(isinstance(payload, dict)
                   and payload.get("schema") == "pramana.swarm_fill.v1"
                   and payload.get("event_type") == "swarm_fill"
                   and payload.get("subject") == entry["symbol"], "committed_entry_receipt_invalid")
            pid, sequence = payload.get("institutional_program"), payload.get("institutional_slice")
            _check(isinstance(pid, str) and type(sequence) is int and 0 < sequence <= 10_000,
                   "committed_entry_attribution_invalid")
            oid = entry["order_id"]
            _check(oid not in seen_orders and (pid, sequence) not in seen_slices,
                   "committed_entry_duplicate_claim")
            seen_orders.add(oid)
            seen_slices.add((pid, sequence))
            _check(payload.get("shared_risk_binding_sha256") == _sha(_raw(pin))
                   and payload.get("order_id") == oid and payload.get("tenant_id") == tenant,
                   "committed_entry_binding_mismatch")
            program = journal.execute("SELECT * FROM execution_programs WHERE program_id=? AND tenant_id=?",
                                      (pid, tenant)).fetchone()
            slice_ = journal.execute("SELECT * FROM execution_program_slices WHERE program_id=? AND sequence=?",
                                     (pid, sequence)).fetchone()
            _check(program is not None and slice_ is not None, "committed_entry_program_missing")
            _check(type(program["risk_authority_version"]) is int and program["risk_authority_version"] == 1,
                   "committed_entry_authority_invalid")
            validate_authority(program["risk_authority_version"], program["risk_authority_payload"],
                program_id=pid, tenant_id=tenant, parent_payload=program["parent_order_payload"],
                runtime_digest=program["runtime_context_sha256"])
            parent = order_from_snapshot(program["parent_order_payload"])
            _check(type(program["parent_quantity"]) is int
                   and program["parent_quantity"] == parent.quantity
                   and program["symbol"] == parent.symbol
                   and program["state"] in {"ACTIVE", "COMPLETE", "FAILED"}
                   and slice_["failure_reason"] is None, "committed_entry_parent_state_mismatch")
            _check(type(slice_["quantity"]) is int and 0 < slice_["quantity"] <= parent.quantity,
                   "committed_entry_quantity_invalid")
            child = replace(parent, quantity=slice_["quantity"])
            receipt = payload.get("paper_submission_receipt")
            _check(isinstance(receipt, dict) and receipt.get("schema") == "pramana.paper_submission_receipt.v1"
                   and receipt.get("orderIntent") == canonical_order_intent(child)
                   and payload.get("institutional_risk_authority_sha256") == program["runtime_context_sha256"],
                   "committed_entry_intent_mismatch")
            _check(child.side is Side.BUY and type(entry["quantity"]) is int
                   and (entry["symbol"], entry["market"], entry["asset_class"], entry["quantity"], entry["instrument_identity"])
                   == (child.symbol, child.market.value, child.asset_class.value, child.quantity, bound_identity(child))
                   and child.asset_class in {AssetClass.EQUITY, AssetClass.ETF}
                   and entry["margin_change"] is None and entry["margin_provenance"] is None
                   and entry["status"] == "FILLED", "committed_entry_ledger_mismatch")
            _check(slice_["state"] in {"DISPATCHING", "FILLED_UNACCOUNTED", "EXECUTED"}
                   and slice_["client_order_id"] == DurableOms.client_order_id(child, f"{program['decision_id']}:slice:{sequence}")
                   and slice_["broker_order_id"] == (None if slice_["state"] == "DISPATCHING" else oid),
                   "committed_entry_state_mismatch")
            key = f"{pid}:{sequence}"
            claim = ledger.execute("SELECT tenant_id FROM paper_idempotency WHERE key=?", (key,)).fetchone()
            _check(payload.get("idempotency_key") == key and claim is not None and claim[0] == tenant,
                   "committed_entry_submission_mismatch")
            at, scheduled = datetime.fromisoformat(entry["created_at"]), datetime.fromisoformat(slice_["scheduled_at"])
            created = datetime.fromisoformat(program["created_at"])
            _check(at.utcoffset() is not None and scheduled.utcoffset() is not None
                   and created.utcoffset() is not None and at >= scheduled and at >= created
                   and datetime.fromisoformat(payload["filled_at"]) == at, "committed_entry_time_mismatch")
            price, notional = Decimal(entry["fill_price"]), Decimal(entry["notional"])
            fill = payload.get("fill")
            _check(price.is_finite() and price > 0 and notional.is_finite() and notional == price * child.quantity
                   and isinstance(fill, dict) and type(fill.get("quantity")) is int
                   and fill["quantity"] == child.quantity
                   and isinstance(fill.get("price"), str) and len(fill["price"]) <= 128
                   and Decimal(fill["price"]) == price
                   and fill.get("status") == "FILLED", "committed_entry_economics_mismatch")
    except SharedRiskError:
        raise
    except (ValueError, TypeError, KeyError, IndexError, ArithmeticError, sqlite3.Error, RecursionError) as error:
        raise SharedRiskError("shared_risk_broker_committed_entry_coverage_invalid") from error
    return len(rows)


def position_linked_capacity(ledger, journal, tenant, *, check_paths=True):
    """Conservative effective shared-risk charge from reconciled broker exposure.

    Reservation rows remain immutable historical evidence. Capacity is released only
    in the derived charge: still-pending/uncertain BUY slices retain their full
    per-unit reservation, while committed exposure is charged against the broker's
    reconciled open quantity. When attribution of a later SELL to an entry programme
    is ambiguous, remaining shares are assigned to the highest per-unit reservation
    first, which can only retain too much risk, never free too much.

    This is internal paper-ledger reconciliation, not an external broker assertion.
    """
    pin = _verify_binding_pair(ledger, journal, tenant, check_paths=check_paths)
    _check(pin is not None, "position_capacity_binding_missing")
    reconciliation = reconcile_paper(ledger, tenant)
    _check(reconciliation.get("status") == "matched", "position_capacity_reconciliation_failed")

    report = verify_shared_risk(journal, tenant)
    raw_total = _decoded_amount(report["reservedLoss"])
    released = {
        row["program_id"]
        for row in _rows(
            journal,
            "SELECT program_id FROM shared_risk_releases WHERE tenant_id=?",
            (tenant,),
        )
    }

    # verify_binding_pair has already checked every committed BUY receipt against
    # the retained programme/slice claim. Build only the quantity index here.
    committed_by_slice = {}
    for row in _rows(
        ledger,
        """SELECT l.quantity,e.payload FROM paper_ledger l
           JOIN paper_decision_evidence e
             ON e.order_id=l.order_id AND e.tenant_id=l.tenant_id
           WHERE l.tenant_id=? AND l.side='BUY' ORDER BY l.id""",
        (tenant,),
    ):
        payload = _source_payload(row["payload"])
        pid, sequence = payload["institutional_program"], payload["institutional_slice"]
        key = (pid, sequence)
        committed_by_slice[key] = row["quantity"]

    pending_risk = Decimal(0)
    committed_capacity = {}
    reservations = _rows(
        journal,
        "SELECT * FROM shared_risk_reservations WHERE tenant_id=? ORDER BY program_id",
        (tenant,),
    )
    for reservation in reservations:
        pid = reservation["program_id"]
        if pid in released:
            continue
        body = _json(reservation["payload"])
        amount = _decoded_amount(body["amount"], positive=True)
        program = journal.execute(
            "SELECT * FROM execution_programs WHERE tenant_id=? AND program_id=?",
            (tenant, pid),
        ).fetchone()
        parent = order_from_snapshot(program["parent_order_payload"])
        per_unit = amount / Decimal(parent.quantity)
        slices = _rows(
            journal,
            """SELECT sequence,quantity,state FROM execution_program_slices
               WHERE program_id=? ORDER BY sequence""",
            (pid,),
        )
        committed = 0
        pending = 0
        for slice_ in slices:
            key = (pid, slice_["sequence"])
            filled = committed_by_slice.get(key)
            state = slice_["state"]
            if filled is not None:
                committed += filled
            elif state in {"PENDING", "DISPATCHING"}:
                # DISPATCHING without a receipt is uncertain, so retain the charge.
                pending += slice_["quantity"]
            else:
                _check(
                    state in {"FAILED", "CANCELLED"},
                    "position_capacity_unrecorded_fill_state_invalid",
                )
        pending_risk += per_unit * Decimal(pending)
        if committed:
            instrument_key = (parent.symbol, parent.market.value, parent.asset_class.value)
            committed_capacity.setdefault(instrument_key, []).append((per_unit, committed, pid))

    positions = {}
    for row in _rows(
        ledger,
        """SELECT symbol,market,asset_class,quantity FROM paper_positions
           WHERE tenant_id=? ORDER BY symbol,market,asset_class""",
        (tenant,),
    ):
        quantity = row["quantity"]
        key = (row["symbol"], row["market"], row["asset_class"])
        positions[key] = quantity

    position_risk = Decimal(0)
    for key, quantity in positions.items():
        candidates = committed_capacity.get(key, ())
        remaining = quantity
        # Max-risk allocation avoids inventing which historical lot a SELL closed.
        for per_unit, committed, _pid in sorted(
            candidates, key=lambda item: (item[0], item[2]), reverse=True
        ):
            used = min(remaining, committed)
            position_risk += per_unit * Decimal(used)
            remaining -= used
            if remaining == 0:
                break

    effective = pending_risk + position_risk
    _check(
        effective.is_finite()
        and effective >= 0
        and effective <= raw_total,
        "position_capacity_exceeds_reservations",
    )
    return {
        "status": "consistent",
        "rawReservedLoss": str(raw_total),
        "effectiveReservedLoss": str(effective),
        "pendingRisk": str(pending_risk),
        "openPositionRisk": str(position_risk),
        "activationAuthorized": False,
        "scope": "reconciled_internal_paper_position; not external_broker_reconciliation",
    }


def pin_account(broker, journal, tenant, *, allow_create, program_id):
    """The journal transaction is held; a later journal rollback leaves entries held.

    This is deliberately not presented as a cross-database atomic commit. The ledger
    witness survives a failed first journal commit and refuses rebootstrap by default.
    """
    _check(journal.in_transaction, "journal_transaction_required")
    report = verify_shared_risk(journal, tenant)
    _check(report["status"] == "consistent" and report.get("journalId") is not None,
           "legacy_journal_requires_migration")
    with broker._lock, broker._connection:
        db = broker._connection
        broker._ensure_account(tenant)
        old = read_binding(db, tenant)
        if old is not None:
            _verify_binding_pair(db, journal, tenant, pending_program_id=program_id)
            record_admission(db, journal, old, program_id)
            verify_binding_pair(db, journal, tenant)
            return _sha(_raw(old))
        _check(allow_create is True and not broker.ledger_entries(tenant)
               and not broker.get_positions(tenant)
               and db.execute("SELECT 1 FROM paper_idempotency WHERE tenant_id=?", (tenant,)).fetchone() is None,
               "legacy_account_requires_migration")
        left, right = database_path(db), database_path(journal)
        _check(left != right and report["policy"]["ledgerKey"] == ledger_key(left, right, tenant),
               "journal_path_mismatch")
        pin = {"schema": SCHEMA, "tenantId": tenant, "accountRef": report["policy"]["accountRef"],
            "currency": report["policy"]["currency"], "journalId": report["journalId"],
            "journalPath": right, "ledgerPath": left, "ledgerKey": ledger_key(left, right, tenant),
            "policySha256": report["policySha256"]}
        raw = _raw(pin)
        # Ordinary replay/legacy ledgers retain their existing table inventory.
        # The optional binding table and pin appear only on explicit successful setup.
        db.execute(f"CREATE TABLE IF NOT EXISTS {TABLE}(tenant_id TEXT PRIMARY KEY,payload TEXT NOT NULL,sha256 TEXT NOT NULL)")
        for action in ("UPDATE", "DELETE"):
            db.execute(f"CREATE TRIGGER IF NOT EXISTS shared_broker_binding_{action.lower()}_blocked BEFORE {action} ON {TABLE} BEGIN SELECT RAISE(ABORT,'Shared broker binding is immutable'); END")
        db.execute(f"INSERT INTO {TABLE}(tenant_id,payload,sha256) VALUES(?,?,?)", (tenant, raw, _sha(raw)))
        db.execute(f"UPDATE paper_accounts SET {COLUMN}=? WHERE tenant_id=?", (_sha(raw), tenant))
        # The optional table is created only for a new explicit binding, never to
        # hide a missing witness table on an already-bound account.
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (ADMISSION_TABLE,)).fetchone() is None:
            create_schema(db)
        record_admission(db, journal, pin, program_id)
        verify_binding_pair(db, journal, tenant)
        return _sha(raw)


def verify_receipt_binding(ledger, tenant, payload, side):
    """Check recorded entry attribution without requiring a writable/online journal.

    Exit recovery stays independent. Legacy unselected accounts cannot be labelled
    as bound by a caller-provided checksum; selected entry receipts must match the
    broker ledger's immutable pin. No capacity is released by this check.
    """
    if side is not Side.BUY:
        return
    pin = read_binding(ledger, tenant)
    expected = None if pin is None else _sha(_raw(pin))
    _check(isinstance(payload, dict)
           and payload.get("shared_risk_binding_sha256") == expected,
           "receipt_binding_mismatch")


def assert_bound_entry(ledger, order, evidence, idempotency_key, now):
    """Only an exact, already-claimed reserved child may enter a pinned paper account."""
    if order.side is not Side.BUY:
        return None
    pin = read_binding(ledger, order.tenant_id)
    if pin is None:
        return None
    _check(isinstance(evidence, dict) and isinstance(evidence.get("institutional_program"), str)
           and type(evidence.get("institutional_slice")) is int
           and evidence["institutional_slice"] > 0, "reserved_child_required")
    pid, sequence = evidence["institutional_program"], evidence["institutional_slice"]
    _check(idempotency_key == f"{pid}:{sequence}", "submission_key_mismatch")
    path = Path(pin["journalPath"])
    _check(path.is_file() and not path.is_symlink() and path.stat().st_nlink == 1,
           "journal_unavailable")
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("BEGIN")
            verify_binding_pair(ledger, db, order.tenant_id)
            program = db.execute("SELECT * FROM execution_programs WHERE program_id=? AND tenant_id=?",
                                 (pid, order.tenant_id)).fetchone()
            slice_ = db.execute("SELECT * FROM execution_program_slices WHERE program_id=? AND sequence=?",
                                (pid, sequence)).fetchone()
            _check(program is not None and slice_ is not None and program["state"] == "ACTIVE"
                   and slice_["state"] == "DISPATCHING" and slice_["broker_order_id"] is None,
                   "unclaimed_or_terminal_child")
            try:
                validate_authority(program["risk_authority_version"], program["risk_authority_payload"],
                    program_id=pid, tenant_id=order.tenant_id,
                    parent_payload=program["parent_order_payload"],
                    runtime_digest=program["runtime_context_sha256"])
                _check(program["risk_authority_version"] == 1, "program_authority_invalid")
            except (TypeError, ValueError, KeyError, IndexError) as error:
                raise SharedRiskError("shared_risk_broker_program_authority_invalid") from error
            parent = order_from_snapshot(program["parent_order_payload"])
            child = replace(parent, quantity=slice_["quantity"])
            _check(type(slice_["quantity"]) is int and child.quantity > 0
                   and canonical_order_intent(child) == canonical_order_intent(order)
                   and slice_["client_order_id"] == DurableOms.client_order_id(child,
                       f"{program['decision_id']}:slice:{sequence}")
                   and evidence.get("institutional_risk_authority_sha256") == program["runtime_context_sha256"],
                   "child_identity_mismatch")
            at = datetime.fromisoformat(slice_["scheduled_at"])
            _check(now.utcoffset() is not None and at.utcoffset() is not None and now >= at,
                   "child_not_due")
            currency = (getattr(order, "instrument", None).currency if getattr(order, "instrument", None)
                        else {Market.INDIA: "INR", Market.USA: "USD"}.get(order.market))
            _check(order.asset_class in {AssetClass.EQUITY, AssetClass.ETF} and currency == pin["currency"],
                   "entry_currency_or_segment_mismatch")
            other = db.execute("""SELECT 1 FROM execution_program_slices s JOIN execution_programs p
                ON p.program_id=s.program_id WHERE p.tenant_id=? AND s.state IN ('DISPATCHING','FILLED_UNACCOUNTED')
                AND NOT (s.program_id=? AND s.sequence=?) LIMIT 1""", (order.tenant_id, pid, sequence)).fetchone()
            _check(other is None, "other_submission_unresolved")
    except (sqlite3.Error, OSError) as error:
        raise SharedRiskError("shared_risk_broker_journal_unavailable") from error
    return _sha(_raw(pin))
