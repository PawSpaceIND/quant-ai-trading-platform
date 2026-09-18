"""Offline verification of captured bound-paper OMS state; never repair or activate.

Use the existing OMS event replay through read-only connections. File hashes bind
captured bytes; stored declarations and local replay are not provider authentication.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from threading import RLock
from types import SimpleNamespace

from quant_ai.governance.runtime_identity import runtime_identity_configuration
from quant_ai.orders.intent import bound_identity, order_from_snapshot
from quant_ai.orders.oms import TERMINAL, DurableOms

SCHEMA = "pramana.oms_recovery_capture.v1"
MAX_ORDERS = 100_000


def _hash(value) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def configuration(path: Path, tenant: str) -> dict | None:
    """Read the saved pin without constructing/migrating a paper broker."""
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError("Recovery ledger must be an unaliased regular file")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("BEGIN")
        # Existing decoder needs only this lock/connection, not a writable broker.
        return runtime_identity_configuration(SimpleNamespace(_connection=db, _lock=RLock()), tenant)


def inspect(ledger: Path, oms_path: Path, tenant: str, source_path_sha256: str) -> dict:
    """Verify one captured account's events and cash-fill correspondence, read-only."""
    binding = configuration(ledger, tenant)
    if binding is None or binding["oms_path_sha256"] != source_path_sha256:
        raise ValueError("Recovery OMS configuration mismatch")
    with DurableOms(oms_path, read_only=True) as oms, closing(sqlite3.connect(
        ledger.resolve().as_uri() + "?mode=ro", uri=True,
    )) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("BEGIN")
        with oms.transaction():
            version = oms.db.execute("SELECT version FROM oms_meta WHERE id=1").fetchone()[0]
            tables = {r[0] for r in oms.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            required = {"oms_meta", "oms_orders", "oms_events", "oms_fills", "oms_replacements", "oms_broker_evidence_bindings"}
            if version == 2:
                required.add("oms_paper_recoveries")
            if tables != required:
                raise ValueError("Recovery OMS schema inventory mismatch")
            if [tuple(row) for row in oms.db.execute("PRAGMA integrity_check")] != [("ok",)]:
                raise ValueError("Recovery OMS integrity failed")
            if oms.db.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("Recovery OMS foreign-key discrepancy")
            for table in ("oms_events", "oms_fills", "oms_broker_evidence_bindings"):
                if oms.db.execute(f"SELECT 1 FROM {table} e LEFT JOIN oms_orders o ON e.client_order_id=o.client_order_id WHERE o.client_order_id IS NULL LIMIT 1").fetchone():
                    raise ValueError("Recovery OMS orphaned history")
            if version == 2 and oms.db.execute("SELECT 1 FROM oms_paper_recoveries r LEFT JOIN oms_orders o ON r.client_order_id=o.client_order_id WHERE o.client_order_id IS NULL OR o.tenant_id<>r.tenant_id LIMIT 1").fetchone():
                raise ValueError("Recovery OMS orphaned recovery audit")
            if oms.db.execute("SELECT 1 FROM oms_replacements r LEFT JOIN oms_orders a ON a.client_order_id=r.original_client_order_id LEFT JOIN oms_orders b ON b.client_order_id=r.replacement_client_order_id WHERE a.client_order_id IS NULL OR b.client_order_id IS NULL OR a.tenant_id<>b.tenant_id LIMIT 1").fetchone():
                raise ValueError("Recovery OMS orphaned replacement")
            audited = set() if version == 1 else {r[0] for r in oms.db.execute(
                "SELECT client_order_id FROM oms_paper_recoveries WHERE tenant_id=?", (tenant,))}
            count = oms.db.execute("SELECT COUNT(*) FROM oms_orders WHERE tenant_id=?", (tenant,)).fetchone()[0]
            if count > MAX_ORDERS:
                raise ValueError("Recovery OMS inventory exceeds bounds")
            raw_orders = oms.db.execute("SELECT requested_quantity,filled_quantity,last_event_sequence FROM oms_orders WHERE tenant_id=?", (tenant,)).fetchall()
            for row in raw_orders:
                if (any(type(value) is not int or not 0 <= value <= 2**53 - 1 for value in row)
                        or row[0] == 0 or row[2] == 0 or row[1] > row[0]):
                    raise ValueError("Recovery OMS projection integers invalid")
            orders = oms.all_orders(tenant)
            heads, fills = {}, {}
            for order in orders:
                checked = oms.verify(order.client_order_id)
                markers = [json.loads(r[0]) for r in oms.db.execute(
                    "SELECT payload FROM oms_events WHERE client_order_id=? AND kind='FILL'", (order.client_order_id,))
                    if "paperRecovery" in json.loads(r[0])]
                if len(markers) != int(order.client_order_id in audited):
                    raise ValueError("Recovery OMS orphaned recovery audit")
                replacement = oms.replacement_for(order.client_order_id)
                replacement_events = list(oms.db.execute(
                    "SELECT 1 FROM oms_events WHERE client_order_id=? AND kind='REPLACED_BY'", (order.client_order_id,)))
                if len(replacement_events) != int(replacement is not None):
                    raise ValueError("Recovery OMS orphaned replacement")
                intent = oms.get_intent(order.client_order_id)
                if (order.instrument_identity != binding["instruments"].get(order.symbol)
                        or intent.tenant_id != tenant or order.market != "INDIA"
                        or order.asset_class not in {"EQUITY", "ETF"}
                        or intent.instrument.currency != "INR" or intent.instrument.exchange != "NSE"):
                    raise ValueError("Recovery OMS bound cash identity mismatch")
                if oms.broker_evidence_binding(order.client_order_id) is not None:
                    raise ValueError("Recovery OMS external broker scope unsupported")
                heads[order.client_order_id] = checked["headHash"]
                for fill in oms.db.execute("SELECT * FROM oms_fills WHERE client_order_id=?", (order.client_order_id,)):
                    fills[fill["fill_id"]] = (order, fill)
            ledger_rows = db.execute("SELECT * FROM paper_ledger WHERE tenant_id=? AND status='FILLED'", (tenant,)).fetchall()
            protected = _protected_fills(db, tenant)
            matched = set()
            unmatched = 0
            for row in ledger_rows:
                if row["order_id"] in protected:
                    continue  # Separate protective-exit recovery remains outside this OMS.
                pair = fills.get(row["order_id"])
                if pair is None:
                    unmatched += 1
                    continue
                order, fill = pair
                if (order.symbol != row["symbol"] or order.market != row["market"]
                        or order.asset_class != row["asset_class"] or order.side != row["side"]
                        or order.instrument_identity != row["instrument_identity"]
                        or fill["quantity"] != row["quantity"]
                        or Decimal(fill["price"]) != Decimal(row["fill_price"])
                        or order.broker_order_id != row["order_id"]
                        or datetime.fromisoformat(fill["at"]) != datetime.fromisoformat(row["created_at"])):
                    raise ValueError("Recovery OMS ledger fill mismatch")
                matched.add(row["order_id"])
            pending = sum(order.state not in TERMINAL for order in orders)
            extra = len(set(fills) - matched)
            return {
                "schema": SCHEMA, "tenant": tenant, "omsVersion": version,
                "configurationSha256": _hash(binding), "eventHeadsSha256": _hash(heads),
                "orders": len(orders), "pendingOrders": pending,
                "matchedLedgerFills": len(matched), "unmatchedLedgerFills": unmatched,
                "unmatchedOmsFills": extra, "protectiveFillsExcluded": len(protected),
                "status": "captured_state_verified" if not pending and not unmatched and not extra else "discrepancy",
                "pathRebindRequired": True, "liveExecutionAuthorized": False,
                "scope": "Captured selected-tenant OMS event replay and stored cash-fill correspondence only; no broker authenticity, protective-exit or separate accounting certification",
            }


def _protected_fills(db, tenant):
    """Do not let a bare outbox row hide an unmatched ordinary ledger fill."""
    present = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    tables = {"paper_protective_fill_outbox", "paper_protection_evidence"}
    if not tables <= present:
        raise ValueError("Recovery protective evidence schema missing")
    rows = db.execute("SELECT order_id,receipt_sha256 FROM paper_protective_fill_outbox WHERE tenant_id=?", (tenant,)).fetchall()
    proofs = {row["order_id"]: row["payload"] for row in db.execute(
        "SELECT order_id,payload FROM paper_protection_evidence WHERE tenant_id=?", (tenant,))}
    if {r["order_id"] for r in rows} != set(proofs):
        raise ValueError("Recovery protective evidence inventory mismatch")
    for row in rows:
        raw = proofs[row["order_id"]]
        if hashlib.sha256(raw.encode()).hexdigest() != row["receipt_sha256"]:
            raise ValueError("Recovery protective evidence hash mismatch")
        evidence = json.loads(raw)
        ledger = db.execute("SELECT * FROM paper_ledger WHERE order_id=? AND tenant_id=?", (row["order_id"], tenant)).fetchone()
        if ledger is None:
            raise ValueError("Recovery protective ledger fill missing")
        receipt = evidence.get("paper_submission_receipt", {})
        intent = order_from_snapshot(receipt.get("orderIntent"))
        if (evidence.get("schema") != "pramana.protective_exit.v1"
                or evidence.get("event_type") != "protective_exit"
                or evidence.get("order_id") != ledger["order_id"]
                or evidence.get("tenant_id") != tenant or evidence.get("subject") != ledger["symbol"]
                or ledger["side"] != "SELL" or ledger["status"] != "FILLED"
                or (intent.tenant_id, intent.symbol, intent.market.value, intent.asset_class.value,
                    intent.side.value, intent.quantity) != (tenant, ledger["symbol"], ledger["market"],
                    ledger["asset_class"], "SELL", ledger["quantity"])
                or bound_identity(intent) != ledger["instrument_identity"]
                or receipt.get("schema") != "pramana.paper_submission_receipt.v1"
                or evidence.get("fill", {}).get("quantity") != ledger["quantity"]
                or Decimal(str(evidence.get("fill", {}).get("price"))) != Decimal(ledger["fill_price"])
                or datetime.fromisoformat(evidence["filled_at"]) != datetime.fromisoformat(ledger["created_at"])):
            raise ValueError("Recovery protective evidence fill mismatch")
    return set(proofs)
