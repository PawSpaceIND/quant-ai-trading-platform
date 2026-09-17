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
from pathlib import Path

from quant_ai.domain.models import AssetClass, Market, Side
from quant_ai.execution.risk_authority import validate_authority
from quant_ai.execution.shared_risk import SharedRiskError, _json, verify_shared_risk
from quant_ai.orders.intent import canonical_order_intent, order_from_snapshot
from quant_ai.orders.oms import DurableOms

TABLE = "paper_shared_risk_bindings"
COLUMN = "shared_risk_binding_sha256"
SCHEMA = "pramana.paper_shared_risk_binding.v1"
SHA = re.compile(r"[0-9a-f]{64}")
JID = re.compile(r"[0-9a-f]{32}")


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
    _check(value["schema"] == SCHEMA and value["tenantId"] == tenant
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
    return pin


def pin_account(broker, journal, tenant, *, allow_create):
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
            from datetime import datetime
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
