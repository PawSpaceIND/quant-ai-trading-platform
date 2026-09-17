"""Offline cash-accounting and execution-program capture verification.

All stores are read-only. No coordinator, broker constructor, model, recovery writer,
order transport, risk override or activation is invoked. This checks saved local
facts, not the unretained full risk context or the authenticity of market evidence.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from quant_ai.accounting.journal import CANONICAL_ACCOUNTS, Account, AccountType, TransactionKind
from quant_ai.instruments.identity import instrument_from_identity
from quant_ai.operations import oms_recovery
from quant_ai.orders.intent import canonical_order_intent, order_from_snapshot
from quant_ai.orders.oms import DurableOms

SCHEMA = "pramana.institutional_recovery_capture.v1"
ROLES = frozenset({"accounting", "programs"})
MAX_ROWS = 100_000
MAX_FILE_BYTES = 256 * 1024 * 1024
_SHA = re.compile(r"[0-9a-f]{64}")
_ID = re.compile(r"[A-Za-z0-9._:-]{1,180}")
TAX_CODES = frozenset({"STT", "CTT", "GST", "STAMP", "SEBI"})
JOURNAL_TABLES = {"trading_journal_meta", "trading_accounts", "trading_transactions", "trading_postings", "sqlite_sequence"}
PROGRAM_TABLES = {"execution_programs", "execution_program_slices"}
D = Decimal


def _sha(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _check(ok, detail):
    if not ok:
        raise ValueError("Institutional recovery " + detail)


def _integer(value, *, positive=False):
    _check(type(value) is int and int(positive) <= value <= 2**53 - 1, "integer invalid")
    return value


def _money(value, *, positive=False):
    _check(isinstance(value, str) and len(value) <= 256, "amount invalid")
    try:
        result = D(value)
    except InvalidOperation as error:
        raise ValueError("Institutional recovery amount invalid") from error
    _check(result is not None and result.is_finite() and (not positive or result > 0), "amount invalid")
    return result


def _instant(raw):
    _check(isinstance(raw, str) and len(raw) <= 64, "timestamp invalid")
    try:
        value = datetime.fromisoformat(raw)
    except ValueError as error:
        raise ValueError("Institutional recovery timestamp invalid") from error
    _check(value.tzinfo is not None and value.utcoffset() is not None, "timestamp invalid")
    return value.astimezone(timezone.utc)


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _check(key not in result, "duplicate JSON key")
            result[key] = value
        return result
    _check(isinstance(raw, str) and len(raw.encode()) <= 1_000_000, "payload too large")
    value = json.loads(raw, object_pairs_hook=unique,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Institutional recovery nonfinite JSON")))
    _check(isinstance(value, dict), "payload invalid")
    return value


@contextmanager
def _database(path: Path, required=None):
    _check(not path.is_symlink() and path.is_file() and path.stat().st_nlink == 1,
           "unaliased existing database required")
    _check(path.stat().st_size <= MAX_FILE_BYTES, "database too large")
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("BEGIN")
        if required is not None:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            _check(tables == required, "schema inventory mismatch")
            _check([tuple(r) for r in db.execute("PRAGMA integrity_check")] == [("ok",)], "integrity failed")
            _check(db.execute("PRAGMA foreign_key_check").fetchone() is None, "foreign-key discrepancy")
        yield db
    finally:
        db.close()


def _rows(db, sql, args=()):
    rows = db.execute(sql, args).fetchmany(MAX_ROWS + 1)
    _check(len(rows) <= MAX_ROWS, "inventory exceeds bounds")
    return rows


def _receipts(db, tenant):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_decision_evidence'").fetchone():
        return {}
    result = {}
    for row in _rows(db, "SELECT order_id,payload FROM paper_decision_evidence WHERE tenant_id=?", (tenant,)):
        payload = _json(row["payload"])
        if "institutional_program" not in payload and "institutional_slice" not in payload:
            continue
        pid, sequence = payload.get("institutional_program"), payload.get("institutional_slice")
        _check(isinstance(pid, str) and _ID.fullmatch(pid), "receipt program invalid")
        _integer(sequence, positive=True)
        key = (pid, sequence)
        _check(key not in result, "duplicate slice receipt")
        _check(payload.get("order_id") == row["order_id"] and payload.get("tenant_id") == tenant,
               "receipt account mismatch")
        result[key] = payload
    return result


def required(ledger: Path, tenant: str) -> bool:
    """Detect recorded institutional fills only; never discover other store paths."""
    with _database(ledger) as db:
        return bool(_receipts(db, tenant))


def _journal(db, tenant):
    meta = db.execute("SELECT id,version,base_currency FROM trading_journal_meta").fetchall()
    _check(len(meta) == 1 and tuple(meta[0]) == (1, 1, "INR"), "INR cash journal required")
    accounts = {}
    for row in _rows(db, "SELECT code,account_type FROM trading_accounts"):
        account = Account(row["code"], AccountType(row["account_type"]))
        accounts[account.code] = account.account_type
    _check(all(accounts.get(a.code) == a.account_type for a in CANONICAL_ACCOUNTS), "account chart mismatch")
    _check(db.execute("SELECT 1 FROM trading_postings p LEFT JOIN trading_transactions t ON p.transaction_id=t.transaction_id WHERE t.transaction_id IS NULL LIMIT 1").fetchone() is None, "orphan posting")
    transactions = {}
    hashes = {}
    balances = {}
    for row in _rows(db, "SELECT * FROM trading_transactions WHERE tenant_id=? ORDER BY at,transaction_id", (tenant,)):
        tid = row["transaction_id"]
        _check(isinstance(tid, str) and _ID.fullmatch(tid), "transaction identity invalid")
        kind = TransactionKind(row["kind"]).value
        at = _instant(row["at"])
        _check(row["at"] == at.isoformat(), "noncanonical journal timestamp")
        _check(isinstance(row["reference"], str) and 0 < len(row["reference"]) <= 500, "transaction reference invalid")
        postings = _rows(db, "SELECT * FROM trading_postings WHERE transaction_id=? ORDER BY sequence", (tid,))
        _check(2 <= len(postings) <= 100, "posting count invalid")
        payload_postings, exact = [], []
        signed = D(0)
        for sequence, item in enumerate(postings, 1):
            _check(type(item["sequence"]) is int and item["sequence"] == sequence, "posting sequence invalid")
            _check(item["account"] in accounts and item["side"] in {"DEBIT", "CREDIT"}, "posting account or side invalid")
            _check(item["currency"] == "INR", "mixed-currency accounting unsupported")
            amount, base = _money(item["amount"], positive=True), _money(item["base_amount"], positive=True)
            _check(base == amount, "base-currency amount mismatch")
            value = amount if item["side"] == "DEBIT" else -amount
            signed += value
            balances[item["account"]] = balances.get(item["account"], D(0)) + value
            exact.append((item["account"], item["side"], amount, base))
            payload_postings.append({"sequence": sequence, "account": item["account"], "side": item["side"],
                                     "currency": "INR", "amount": str(amount), "baseAmount": str(base)})
        _check(signed == 0, "unbalanced accounting transaction")
        payload = {"transactionId": tid, "tenantId": tenant, "kind": kind, "reference": row["reference"],
                   "at": at.isoformat(), "postings": payload_postings}
        digest = _sha(payload)
        _check(row["payload_sha256"] == digest, "accounting transaction hash mismatch")
        hashes[tid] = digest
        transactions[tid] = (kind, row["reference"], at, tuple(exact))
    return transactions, hashes, balances


def _cash_book(db, tenant, binding):
    accounts = db.execute("SELECT starting_capital,cash_balance FROM paper_accounts WHERE tenant_id=?", (tenant,)).fetchall()
    _check(len(accounts) == 1, "paper account missing")
    starting = _money(accounts[0]["starting_capital"], positive=True)
    cash = _money(accounts[0]["cash_balance"])
    entries = _rows(db, "SELECT * FROM paper_ledger WHERE tenant_id=? ORDER BY id", (tenant,))
    costs = _rows(db, "SELECT * FROM paper_cost_ledger WHERE tenant_id=? ORDER BY id", (tenant,))
    expected, by_id, prior, positions = {}, {}, {}, {}
    expected_cash = starting
    for row in entries:
        oid = row["order_id"]
        _check(oid not in by_id and isinstance(oid, str) and _ID.fullmatch(oid), "ledger order identity invalid")
        _check(row["status"] == "FILLED" and row["market"] == "INDIA" and row["asset_class"] in {"EQUITY", "ETF"}
               and row["margin_change"] is None and row["instrument_identity"] == binding["instruments"].get(row["symbol"]),
               "bound cash ledger required")
        _check(row["instrument_identity"] is not None, "bound cash ledger required")
        qty, price, notional = _integer(row["quantity"], positive=True), _money(row["fill_price"], positive=True), _money(row["notional"], positive=True)
        _check(notional == qty * price, "ledger notional mismatch")
        at = _instant(row["created_at"])
        held, average = positions.get(row["symbol"], (0, D(0)))
        prior[oid] = (held, average if held else None, row["instrument_identity"] if held else None)
        if row["side"] == "BUY":
            lines = [("SECURITIES_COST", "DEBIT", notional, notional), ("CASH_AVAILABLE", "CREDIT", notional, notional)]
            expected_cash -= notional
            average = (average * held + notional) / (held + qty)
            held += qty
        else:
            _check(row["side"] == "SELL" and qty <= held, "sale without held position")
            basis, pnl = average * qty, notional - average * qty
            lines = [("CASH_AVAILABLE", "DEBIT", notional, notional), ("SECURITIES_COST", "CREDIT", basis, basis)]
            if pnl:
                lines.append(("REALIZED_PNL", "CREDIT" if pnl > 0 else "DEBIT", abs(pnl), abs(pnl)))
            expected_cash += notional
            held -= qty
        positions[row["symbol"]] = (held, average if held else D(0))
        expected["trade:" + oid] = ("TRADE", f"paper fill {oid} {row['symbol']}", at, tuple(lines))
        by_id[oid] = row
    fee_totals, cost_keys = {}, set()
    for row in costs:
        _check(row["order_id"] in by_id, "orphan ledger fee")
        _check(type(row["cash_debit"]) is int and row["cash_debit"] in {0, 1}, "fee flag invalid")
        amount = _money(row["amount"], positive=True)
        code, oid = row["code"], row["order_id"]
        _check(isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code), "fee code invalid")
        _check((oid, code) not in cost_keys, "duplicate ledger fee")
        cost_keys.add((oid, code))
        if not row["cash_debit"]:
            continue  # Spread/slippage are already reflected in the recorded fill price.
        tax = code in TAX_CODES
        reference = f"paper fill {oid} {by_id[oid]['symbol']} cost {code}"
        expected[f"cost:{oid}:{code}"] = ("TAX" if tax else "FEE", reference, _instant(row["created_at"]), (
            ("TAX_EXPENSE" if tax else "FEE_EXPENSE", "DEBIT", amount, amount),
            ("CASH_AVAILABLE", "CREDIT", amount, amount)))
        expected_cash -= amount
        fee_totals[oid] = fee_totals.get(oid, D(0)) + amount
    return expected, by_id, prior, fee_totals, starting, cash, expected_cash


def _accounting(db, tenant, expected, starting, cash, expected_cash):
    transactions, hashes, balances = _journal(db, tenant)
    capital = D(0)
    for tid, saved in transactions.items():
        if tid in expected:
            _check(saved == expected[tid], "recorded posting economics mismatch")
            continue
        kind, _reference, _at, lines = saved
        _check(kind == "CAPITAL" and len(lines) == 2 and [p[:2] for p in lines] == [
            ("CASH_AVAILABLE", "DEBIT"), ("CAPITAL", "CREDIT")], "unmapped accounting transaction")
        capital += lines[0][2]
    missing = set(expected) - set(transactions)
    discrepancies = (capital != starting) + (cash != expected_cash) + (balances.get("CASH_AVAILABLE", D(0)) != cash)
    return {"transactions": len(transactions), "transactionsSha256": _sha(hashes),
            "missingTransactions": len(missing), "balanceDiscrepancies": discrepancies,
            "status": "consistent" if not missing and not discrepancies else "discrepancy"}


def _validate_receipt(db, payload, child, pid, sequence, row, prior, fee_total):
    raw = canonical_order_intent(child)
    receipt = payload.get("paper_submission_receipt")
    _check(isinstance(receipt, dict) and receipt.get("schema") == "pramana.paper_submission_receipt.v1", "receipt missing")
    _check(payload.get("schema") == "pramana.swarm_fill.v1" and payload.get("event_type") == "swarm_fill"
           and payload.get("institutional_program") == pid and payload.get("institutional_slice") == sequence
           and payload.get("order_intent_sha256") == hashlib.sha256(raw.encode()).hexdigest()
           and payload.get("instrument_identity") == row["instrument_identity"]
           and receipt.get("orderIntent") == raw, "receipt parent or decision mismatch")
    _check((child.tenant_id, child.symbol, child.market.value, child.asset_class.value, child.side.value, child.quantity)
           == (row["tenant_id"], row["symbol"], row["market"], row["asset_class"], row["side"], row["quantity"]),
           "receipt ledger identity mismatch")
    _check(payload.get("subject") == row["symbol"] and _instant(payload.get("filled_at")) == _instant(row["created_at"]), "receipt timestamp mismatch")
    fill = payload.get("fill", {})
    _check(type(fill.get("quantity")) is int and fill["quantity"] == row["quantity"]
           and fill.get("status") == "FILLED" and _money(fill.get("price"), positive=True) == _money(row["fill_price"])
           and _money(fill.get("cash_fees")) == fee_total, "receipt economics mismatch")
    _check(type(receipt.get("priorQuantity")) is int and receipt["priorQuantity"] == prior[0]
           and (None if receipt.get("priorAverage") is None else _money(receipt["priorAverage"], positive=True)) == prior[1]
           and receipt.get("priorInstrumentIdentity") == prior[2], "receipt historical cost mismatch")
    key = f"{pid}:{sequence}"
    claim = db.execute("SELECT tenant_id FROM paper_idempotency WHERE key=?", (key,)).fetchone()
    _check(payload.get("idempotency_key") == key and claim is not None and claim[0] == child.tenant_id, "receipt idempotency mismatch")


def _programs(db, ledger, oms, tenant, binding, entries, prior, fees, accounting_db):
    _check(db.execute("SELECT 1 FROM execution_program_slices s LEFT JOIN execution_programs p ON s.program_id=p.program_id WHERE p.program_id IS NULL LIMIT 1").fetchone() is None, "orphan program slice")
    receipts = _receipts(ledger, tenant)
    matched, claimed_clients, claimed_fills, snapshot = set(), set(), set(), []
    counts = {"programs": 0, "slices": 0, "unresolvedSlices": 0, "failedOrCancelledPrograms": 0, "receipts": len(receipts)}
    for program in _rows(db, "SELECT * FROM execution_programs WHERE tenant_id=? ORDER BY program_id", (tenant,)):
        for field in ("program_id", "decision_id", "symbol"):
            _check(isinstance(program[field], str) and _ID.fullmatch(program[field]), "program identity invalid")
        for field in ("plan_sha256", "runtime_context_sha256"):
            _check(isinstance(program[field], str) and _SHA.fullmatch(program[field]), "program digest invalid")
        parent = order_from_snapshot(program["parent_order_payload"])
        quantity = _integer(program["parent_quantity"], positive=True)
        _check((parent.tenant_id, parent.symbol, parent.quantity) == (tenant, program["symbol"], quantity)
               and getattr(parent, "instrument", None) is not None and parent.market.value == "INDIA"
               and parent.asset_class.value in {"EQUITY", "ETF"} and parent.instrument.currency == "INR"
               and parent.instrument.exchange == "NSE"
               and json.loads(canonical_order_intent(parent))["instrumentIdentity"] == binding["instruments"].get(parent.symbol),
               "program parent identity mismatch")
        _instant(program["created_at"])
        pid = program["program_id"]
        slices = _rows(db, "SELECT * FROM execution_program_slices WHERE program_id=? ORDER BY sequence", (pid,))
        _check(0 < len(slices) <= 10_000, "program slice count invalid")
        _check(sum(_integer(s["quantity"], positive=True) for s in slices) == quantity, "program parent conservation mismatch")
        states, previous_time = [], None
        for sequence, item in enumerate(slices, 1):
            _check(type(item["sequence"]) is int and item["sequence"] == sequence, "program slice sequence invalid")
            at = _instant(item["scheduled_at"])
            _check(previous_time is None or at >= previous_time, "program schedule out of order")
            previous_time = at
            state = item["state"]
            _check(state in {"PENDING", "DISPATCHING", "FILLED_UNACCOUNTED", "EXECUTED", "FAILED", "CANCELLED"}, "slice state invalid")
            states.append(state)
            child = replace(parent, quantity=item["quantity"])
            cid = DurableOms.client_order_id(child, f"{program['decision_id']}:slice:{sequence}")
            _check(item["client_order_id"] in {None, cid}, "slice client identity mismatch")
            if state in {"DISPATCHING", "FILLED_UNACCOUNTED", "EXECUTED"}:
                _check(item["client_order_id"] == cid, "slice dispatch identity missing")
            if state == "PENDING":
                _check(item["client_order_id"] is None and item["broker_order_id"] is None
                       and item["pre_fill_average_price"] is None and item["failure_reason"] is None, "pending slice metadata inconsistent")
            if state == "DISPATCHING":
                _check(item["broker_order_id"] is None and item["pre_fill_average_price"] is None
                       and item["failure_reason"] is None, "dispatch metadata inconsistent")
            if state in {"FILLED_UNACCOUNTED", "EXECUTED"}:
                _check(item["failure_reason"] is None, "completed slice has failure metadata")
            if state == "FAILED":
                _check(isinstance(item["failure_reason"], str) and item["failure_reason"].strip(), "slice failure reason missing")
            _check(cid not in claimed_clients, "duplicate OMS claim")
            claimed_clients.add(cid)
            payload = receipts.get((pid, sequence))
            if payload is not None:
                oid = payload["order_id"]
                _check(oid in entries and oid not in claimed_fills, "duplicate or absent ledger fill")
                _check(state not in {"FAILED", "CANCELLED"}, "terminal slice contradicts committed fill")
                _validate_receipt(ledger, payload, child, pid, sequence, entries[oid], prior[oid], fees.get(oid, D(0)))
                _check(_instant(entries[oid]["created_at"]) >= at, "fill precedes scheduled slice")
                claimed_fills.add(oid)
                matched.add((pid, sequence))
            if state in {"FILLED_UNACCOUNTED", "EXECUTED"}:
                _check(payload is not None and item["broker_order_id"] == payload["order_id"], "completed slice receipt missing")
                average = None if item["pre_fill_average_price"] is None else _money(item["pre_fill_average_price"], positive=True)
                _check(average == prior[payload["order_id"]][1], "slice historical cost mismatch")
            try:
                current = oms.get(cid)
            except KeyError:
                _check(state not in {"FILLED_UNACCOUNTED", "EXECUTED"}, "completed slice OMS missing")
            else:
                oms.verify(cid)
                _check(oms.intent_snapshot(cid) == canonical_order_intent(child), "slice OMS intent mismatch")
                if state in {"FILLED_UNACCOUNTED", "EXECUTED"}:
                    _check(current.state.value == "FILLED" and current.broker_order_id == item["broker_order_id"], "completed slice OMS mismatch")
            if state != "EXECUTED":
                counts["unresolvedSlices"] += 1
            if state == "EXECUTED":
                posted = accounting_db.execute("SELECT 1 FROM trading_transactions WHERE tenant_id=? AND transaction_id=?", (tenant, "trade:" + item["broker_order_id"])).fetchone()
                _check(posted is not None, "executed slice accounting missing")
        actual_state = program["state"]
        _check(actual_state in {"PLANNED", "ACTIVE", "COMPLETE", "FAILED", "CANCELLED"}, "program state invalid")
        _check((actual_state == "COMPLETE") == all(s == "EXECUTED" for s in states), "program completion mismatch")
        if actual_state == "PLANNED":
            _check(all(s == "PENDING" for s in states), "planned program state mismatch")
        if actual_state == "FAILED":
            _check("FAILED" in states, "failed program state mismatch")
        if actual_state == "CANCELLED":
            _check("CANCELLED" in states, "cancelled program state mismatch")
        if actual_state == "ACTIVE":
            _check(any(s in {"DISPATCHING", "FILLED_UNACCOUNTED", "EXECUTED"} for s in states)
                   and not any(s in {"FAILED", "CANCELLED"} for s in states), "active program state mismatch")
        counts["programs"] += 1
        counts["slices"] += len(slices)
        counts["failedOrCancelledPrograms"] += actual_state in {"FAILED", "CANCELLED"}
        snapshot.append({"program": dict(program), "slices": [dict(s) for s in slices]})
    _check(matched == set(receipts), "orphan institutional receipt")
    counts.update({"snapshotSha256": _sha(snapshot), "receiptInventorySha256": _sha({str(k): _sha(v) for k, v in receipts.items()})})
    return counts


def inspect(ledger: Path, oms_path: Path, accounting: Path, programs: Path, tenant: str) -> dict:
    """Verify a selected captured multi-store INR cash account; do not resume it."""
    binding = oms_recovery.configuration(ledger, tenant)
    _check(binding is not None, "bound account required")
    for raw in binding["instruments"].values():
        instrument = instrument_from_identity(raw)
        _check(instrument.market.value == "INDIA" and instrument.asset_class.value in {"EQUITY", "ETF"}
               and instrument.currency == "INR" and instrument.exchange == "NSE", "NSE INR cash scope required")
    with _database(ledger) as paper, _database(accounting, JOURNAL_TABLES) as journal, _database(programs, PROGRAM_TABLES) as schedule, DurableOms(oms_path, read_only=True) as oms, oms.transaction():
        expected, entries, prior, fees, starting, cash, expected_cash = _cash_book(paper, tenant, binding)
        accounting_result = _accounting(journal, tenant, expected, starting, cash, expected_cash)
        program_result = _programs(schedule, paper, oms, tenant, binding, entries, prior, fees, journal)
    discrepancy = accounting_result["status"] == "discrepancy" or program_result["unresolvedSlices"] > 0
    return {"schema": SCHEMA, "tenant": tenant,
            "status": "discrepancy" if discrepancy else "captured_state_consistent",
            "accounting": accounting_result, "programs": program_result,
            "activationAuthorized": False, "runtimeContextVerified": False, "planEvidenceVerified": False,
            "scope": "Selected local INR cash postings, saved parent/slice state and receipt/OMS correspondence only; no risk-context reconstruction, automatic resume, accounting repair or market authenticity"}
