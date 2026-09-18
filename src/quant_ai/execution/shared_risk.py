"""Conservative same-journal reservations for explicitly configured paper accounts.

Reservation rows remain immutable after partial/complete fills and uncertain dispatches.
Never-claimed cancellation retains its explicit release record. The integrated
institutional path may derive a smaller effective charge only from separately
verified broker/journal state; this module never guesses position release itself.
No real account limit, FX rate or external broker evidence is inferred.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from quant_ai.decision.edge import EdgeEvidence
from quant_ai.domain.models import Side
from quant_ai.execution.risk_authority import parse_authority
from quant_ai.orders.intent import order_from_snapshot

D = Decimal
TABLES = frozenset({"shared_risk_accounts", "shared_risk_reservations", "shared_risk_releases"})
_ID = re.compile(r"[A-Za-z0-9._:-]{1,180}")
_SHA = re.compile(r"[0-9a-f]{64}")
MAX_RECORDS = 100000


class SharedRiskError(ValueError):
    """A refusal, not permission to submit without a reservation."""


def _check(condition, reason):
    if not condition:
        raise SharedRiskError("shared_risk_" + reason)


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def _amount(value, *, positive=False):
    _check(isinstance(value, Decimal) and value.is_finite() and len(str(value)) <= 128
           and (value > 0 if positive else value >= 0) and value <= D("1e24"), "amount_invalid")
    return value


def _decoded_amount(raw, *, positive=False):
    _check(isinstance(raw, str) and 0 < len(raw) <= 128, "stored_amount_invalid")
    try:
        value = D(raw)
    except ArithmeticError as error:
        raise SharedRiskError("shared_risk_stored_amount_invalid") from error
    _amount(value, positive=positive)
    _check(str(value) == raw, "stored_amount_noncanonical")
    return value


def _text(value):
    _check(isinstance(value, str) and _ID.fullmatch(value) is not None, "identity_invalid")


def _time(value):
    _check(isinstance(value, datetime) and value.tzinfo is not None
           and value.utcoffset() is not None, "aware_time_required")
    return value.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True)
class SharedRiskPolicy:
    account_ref: str
    currency: str
    max_loss_fraction: Decimal
    revision: str

    def __post_init__(self):
        _text(self.account_ref)
        _text(self.revision)
        _check(self.currency in {"INR", "USD"}, "single_currency_required")
        _amount(self.max_loss_fraction, positive=True)
        _check(self.max_loss_fraction <= 1, "fraction_invalid")

    def payload(self, tenant_id: str, ledger_key: str) -> str:
        replace(self)
        _text(tenant_id)
        _check(isinstance(ledger_key, str) and _SHA.fullmatch(ledger_key), "ledger_binding_invalid")
        return _canonical({"schema": "pramana.shared_risk_policy.v1", "tenantId": tenant_id,
            "accountRef": self.account_ref, "currency": self.currency,
            "maxLossFraction": str(self.max_loss_fraction), "revision": self.revision,
            "ledgerKey": ledger_key})


def _json(raw):
    def unique(pairs):
        obj = {}
        for key, value in pairs:
            _check(key not in obj, "duplicate_json_field")
            obj[key] = value
        return obj
    _check(isinstance(raw, str) and len(raw) <= 8192, "payload_invalid")
    try:
        value = json.loads(raw, object_pairs_hook=unique)
        _check(_canonical(value) == raw, "noncanonical_payload")
        return value
    except (ValueError, TypeError, RecursionError) as error:
        raise SharedRiskError("shared_risk_payload_invalid") from error


def _rows(db, query, parameters=()):
    result = db.execute(query, parameters).fetchmany(MAX_RECORDS + 1)
    _check(len(result) <= MAX_RECORDS, "inventory_limit")
    return result


def selected(db, tenant_id):
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    existing = tables & TABLES
    _check(not existing or existing == TABLES, "schema_incomplete")
    return bool(existing) and db.execute("SELECT 1 FROM shared_risk_accounts WHERE tenant_id=?", (tenant_id,)).fetchone() is not None


def verify_shared_risk(db, tenant_id, *, allow_missing=None):
    """Read-only replay used by the coordinator and offline recovery verifier."""
    if not selected(db, tenant_id):
        return {"status": "not_selected", "reservedLoss": "0", "activationAuthorized": False}
    row = db.execute("SELECT * FROM shared_risk_accounts WHERE tenant_id=?", (tenant_id,)).fetchone()
    columns = set(row.keys())
    journal_id = row["journal_id"] if "journal_id" in columns else None
    _check(journal_id is None or (isinstance(journal_id, str)
           and re.fullmatch(r"[0-9a-f]{32}", journal_id)), "journal_identity_invalid")
    policy = _json(row["payload"])
    _check(set(policy) == {"schema", "tenantId", "accountRef", "currency", "maxLossFraction", "revision", "ledgerKey"}, "policy_fields_invalid")
    expected = SharedRiskPolicy(policy["accountRef"], policy["currency"],
        _decoded_amount(policy["maxLossFraction"], positive=True), policy["revision"]).payload(tenant_id, policy["ledgerKey"])
    _check(row["payload"] == expected and row["sha256"] == _sha(expected)
           and row["account_ref"] == policy["accountRef"], "policy_binding_mismatch")
    reservations = {}
    for item in _rows(db, "SELECT * FROM shared_risk_reservations WHERE tenant_id=? ORDER BY program_id", (tenant_id,)):
        body = _json(item["payload"])
        _check(set(body) == {"schema", "tenantId", "programId", "policySha256", "authoritySha256", "amount", "adverseReturn", "costReturn", "currency", "createdAt"}, "reservation_fields_invalid")
        _check(body["schema"] == "pramana.shared_risk_reservation.v1" and item["sha256"] == _sha(item["payload"])
               and body["tenantId"] == tenant_id and body["programId"] == item["program_id"]
               and body["policySha256"] == row["sha256"] and body["currency"] == policy["currency"], "reservation_binding_mismatch")
        amount = _decoded_amount(body["amount"], positive=True)
        _check(_time(datetime.fromisoformat(body["createdAt"])) == body["createdAt"], "reservation_time_invalid")
        reservations[item["program_id"]] = (amount, item["sha256"], body)
    releases = {}
    for item in _rows(db, "SELECT * FROM shared_risk_releases WHERE tenant_id=?", (tenant_id,)):
        body = _json(item["payload"])
        _check(set(body) == {"schema", "tenantId", "programId", "reservationSha256", "reason", "releasedAt"}, "release_fields_invalid")
        pid = item["program_id"]
        _check(pid in reservations and item["sha256"] == _sha(item["payload"])
               and body["schema"] == "pramana.shared_risk_release.v1"
               and body["tenantId"] == tenant_id and body["programId"] == pid
               and body["reservationSha256"] == reservations[pid][1]
               and body["reason"] == "never_claimed_cancellation", "release_binding_mismatch")
        _check(_time(datetime.fromisoformat(body["releasedAt"])) == body["releasedAt"], "release_time_invalid")
        _check(datetime.fromisoformat(body["releasedAt"]) >= datetime.fromisoformat(reservations[pid][2]["createdAt"]), "release_time_regressed")
        releases[pid] = body
    entries = set()
    for program in _rows(db, "SELECT * FROM execution_programs WHERE tenant_id=?", (tenant_id,)):
        parent = order_from_snapshot(program["parent_order_payload"])
        if parent.side is not Side.BUY:
            continue
        pid = program["program_id"]
        entries.add(pid)
        if pid == allow_missing and pid not in reservations:
            continue
        _check(pid in reservations, "reservation_missing")
        value, _digest, body = reservations[pid]
        authority = parse_authority(program["risk_authority_payload"])
        _check(authority["mode"] == "ENTRY" and body["authoritySha256"] == program["runtime_context_sha256"]
               and _sha(program["risk_authority_payload"]) == body["authoritySha256"]
               and authority["parentSha256"] == _sha(program["parent_order_payload"])
               and authority["tenantId"] == tenant_id and authority["programId"] == pid, "program_authority_mismatch")
        _amount(parent.reference_price, positive=True)
        _amount(parent.stop_price, positive=True)
        _check(type(parent.quantity) is int and parent.quantity > 0, "parent_quantity_invalid")
        adverse = _decoded_amount(body["adverseReturn"], positive=True)
        cost = _decoded_amount(body["costReturn"])
        modeled = max(abs(parent.reference_price - parent.stop_price) * parent.quantity,
                      parent.reference_price * parent.quantity * (adverse + cost))
        _check(value == modeled, "reservation_model_mismatch")
        if pid in releases:
            slices = _rows(db, "SELECT * FROM execution_program_slices WHERE program_id=?", (pid,))
            _check(program["state"] == "CANCELLED" and bool(slices)
                   and all(s["state"] == "CANCELLED" and s["client_order_id"] is None
                           and s["broker_order_id"] is None for s in slices), "released_program_not_unsubmitted")
    _check(set(reservations) <= entries, "orphan_reservation")
    active = {pid: value for pid, (value, _digest, _body) in reservations.items() if pid not in releases}
    total = sum(active.values(), D(0))
    _amount(total)
    return {"status": "consistent", "policy": policy, "policySha256": row["sha256"], "journalId": journal_id,
        "reservedLoss": str(total), "activeReservations": len(active), "releasedReservations": len(releases),
        "activationAuthorized": False}


class SharedRiskReservations:
    def __init__(self, journal):
        self.journal = journal
        self.db = journal.db

    def _schema(self):
        # Called inside the program transaction, not executescript (which can commit).
        for name, body in (
            ("shared_risk_accounts", "tenant_id TEXT PRIMARY KEY,account_ref TEXT UNIQUE NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL,journal_id TEXT NOT NULL"),
            ("shared_risk_reservations", "program_id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL"),
            ("shared_risk_releases", "program_id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL")):
            self.db.execute(f"CREATE TABLE IF NOT EXISTS {name}({body})")
            for action in ("UPDATE", "DELETE"):
                self.db.execute(f"CREATE TRIGGER IF NOT EXISTS {name}_{action.lower()}_blocked BEFORE {action} ON {name} BEGIN SELECT RAISE(ABORT,'Shared risk evidence is immutable'); END")

    def bind(self, policy, *, tenant_id, ledger_key, empty_ledger):
        _check(self.db.in_transaction, "transaction_required")
        _check(isinstance(policy, SharedRiskPolicy), "configuration_required")
        raw = policy.payload(tenant_id, ledger_key)
        selected(self.db, tenant_id)  # Reject partial metadata; do not silently repair it.
        self._schema()
        saved = self.db.execute("SELECT * FROM shared_risk_accounts WHERE tenant_id=?", (tenant_id,)).fetchone()
        if saved is None:
            _check(empty_ledger is True and self.db.execute("SELECT 1 FROM execution_programs WHERE tenant_id=? LIMIT 1", (tenant_id,)).fetchone() is None, "legacy_account_requires_migration")
            _check(self.db.execute("SELECT 1 FROM shared_risk_accounts WHERE account_ref=?", (policy.account_ref,)).fetchone() is None, "account_alias_refused")
            columns = {r[1] for r in self.db.execute("PRAGMA table_info(shared_risk_accounts)")}
            _check("journal_id" in columns, "legacy_journal_requires_migration")
            self.db.execute("INSERT INTO shared_risk_accounts VALUES(?,?,?,?,?)",
                            (tenant_id, policy.account_ref, raw, _sha(raw), uuid4().hex))
        else:
            _check(saved["payload"] == raw and saved["sha256"] == _sha(raw), "configuration_changed")

    def reserve(
        self, program, *, evidence, equity, currency, at,
        existing_reserved_loss: Decimal | None = None,
    ):
        _check(self.db.in_transaction, "transaction_required")
        _check(isinstance(evidence, EdgeEvidence), "evidence_required")
        replace(evidence)
        parent = order_from_snapshot(program.parent_order_payload)
        amount = max(abs(parent.reference_price - parent.stop_price) * parent.quantity,
                     parent.reference_price * parent.quantity * (evidence.average_loss_return + evidence.expected_cost_return))
        _amount(amount, positive=True)
        report = verify_shared_risk(self.db, program.tenant_id, allow_missing=program.program_id)
        _check(report["status"] == "consistent", "configuration_missing")
        _check(currency == report["policy"]["currency"], "currency_mismatch")
        body = {"schema": "pramana.shared_risk_reservation.v1", "tenantId": program.tenant_id,
            "programId": program.program_id, "policySha256": report["policySha256"],
            "authoritySha256": program.runtime_context_sha256, "amount": str(amount),
            "adverseReturn": str(evidence.average_loss_return), "costReturn": str(evidence.expected_cost_return),
            "currency": currency, "createdAt": _time(at)}
        raw = _canonical(body)
        saved = self.db.execute("SELECT * FROM shared_risk_reservations WHERE program_id=?", (program.program_id,)).fetchone()
        if saved is not None:
            _check(self.db.execute("SELECT 1 FROM shared_risk_releases WHERE program_id=?", (program.program_id,)).fetchone() is None, "released_decision_cannot_restart")
            _check(saved["payload"] == raw and saved["sha256"] == _sha(raw), "repeated_reservation_changed")
        else:
            raw_existing = _decoded_amount(report["reservedLoss"])
            existing = raw_existing
            if existing_reserved_loss is not None:
                existing = _amount(existing_reserved_loss)
                _check(existing <= raw_existing, "position_capacity_exceeds_reservations")
            total = existing + amount
            self._capacity(report, equity, total)
            self.db.execute("INSERT INTO shared_risk_reservations VALUES(?,?,?,?)", (program.program_id, program.tenant_id, raw, _sha(raw)))
        return verify_shared_risk(self.db, program.tenant_id)

    @staticmethod
    def _capacity(report, equity, total):
        _amount(equity, positive=True)
        _check(total <= equity * _decoded_amount(report["policy"]["maxLossFraction"], positive=True), "budget_exceeded")

    def check(
        self, program, *, policy, ledger_key, currency, equity, evidence,
        effective_reserved_loss: Decimal | None = None,
    ):
        with self.journal.transaction():
            configured = selected(self.db, program.tenant_id)
            if not configured and policy is None:
                return
            _check(configured and isinstance(policy, SharedRiskPolicy), "configuration_required")
            report = verify_shared_risk(self.db, program.tenant_id)
            _check(report["policySha256"] == _sha(policy.payload(program.tenant_id, ledger_key)), "configuration_changed")
            _check(currency == report["policy"]["currency"], "currency_mismatch")
            charge = self.db.execute("SELECT payload FROM shared_risk_reservations WHERE program_id=? AND tenant_id=?", (program.program_id, program.tenant_id)).fetchone()
            _check(charge is not None and isinstance(evidence, EdgeEvidence), "reservation_missing")
            value = _json(charge[0])
            _check(value["adverseReturn"] == str(evidence.average_loss_return)
                   and value["costReturn"] == str(evidence.expected_cost_return), "reservation_evidence_mismatch")
            raw_reserved = _decoded_amount(report["reservedLoss"])
            effective = raw_reserved
            if effective_reserved_loss is not None:
                effective = _amount(effective_reserved_loss)
                _check(effective <= raw_reserved, "position_capacity_exceeds_reservations")
            self._capacity(report, equity, effective)

    def cancel_unclaimed(self, program_id, *, tenant_id, at):
        with self.journal.transaction():
            verify_shared_risk(self.db, tenant_id)
            program = self.journal.get(program_id)
            _check(program.tenant_id == tenant_id, "tenant_mismatch")
            reservation = self.db.execute("SELECT * FROM shared_risk_reservations WHERE program_id=? AND tenant_id=?", (program_id, tenant_id)).fetchone()
            _check(reservation is not None, "reservation_missing")
            if self.db.execute("SELECT 1 FROM shared_risk_releases WHERE program_id=?", (program_id,)).fetchone():
                return verify_shared_risk(self.db, tenant_id)
            _check(program.state.value == "PLANNED" and bool(program.slices)
                   and all(s.state.value == "PENDING" and s.client_order_id is None and s.broker_order_id is None for s in program.slices), "claimed_reservation_cannot_release")
            _check(at >= program.created_at, "release_time_regressed")
            raw = _canonical({"schema": "pramana.shared_risk_release.v1", "tenantId": tenant_id,
                "programId": program_id, "reservationSha256": reservation["sha256"],
                "reason": "never_claimed_cancellation", "releasedAt": _time(at)})
            self.db.execute("UPDATE execution_program_slices SET state='CANCELLED' WHERE program_id=?", (program_id,))
            self.db.execute("UPDATE execution_programs SET state='CANCELLED' WHERE program_id=?", (program_id,))
            self.db.execute("INSERT INTO shared_risk_releases VALUES(?,?,?,?)", (program_id, tenant_id, raw, _sha(raw)))
            return verify_shared_risk(self.db, tenant_id)
