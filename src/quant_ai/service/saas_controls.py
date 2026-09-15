"""Internal SaaS admission primitives; NOT authentication, billing or broker execution.

Call only behind a verified server session. Tenant IDs, limits and grants must never
come from untrusted request claims. This database is separate from trading ledgers.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

PURPOSES = frozenset({"private_research", "simulation", "customer_display", "model_input"})


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
        raise ValueError("invalid_identifier")
    return value


def _amount(value):
    if type(value) is not int or not 0 <= value <= 10**12:
        raise ValueError("invalid_micro_usd_amount")
    return value


def _time(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("aware_time_required")
    return value.astimezone(timezone.utc)


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError("sha256_required")
    return value


class SaaSControls:
    """Trusted control-plane API. No default subscription or data grant is created.

    Integer micro-USD avoids floating-point accounting. BEGIN IMMEDIATE serializes
    admission across processes. Unsettled requests keep their entire reservation,
    including after timeouts/restarts; retries never authorize a second API call.
    """

    def __init__(self, path, *, readonly=False):
        self.readonly = readonly
        if readonly:
            uri = "file:" + quote(str(Path(path).resolve()), safe="/") + "?mode=ro"
            self.db = sqlite3.connect(uri, uri=True, timeout=10, isolation_level=None)
        else:
            self.db = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        expected = {"saas_accounts", "saas_grants", "saas_usage", "saas_audit"}
        tables = {r[0] for r in self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )}
        if (tables and tables != expected) or (readonly and tables != expected):
            self.db.close()
            raise ValueError("not_a_saas_controls_database")
        self.db.execute("PRAGMA foreign_keys=ON")
        if readonly:
            self.db.execute("PRAGMA query_only=ON")
            return
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS saas_accounts (
          tenant TEXT PRIMARY KEY, active INTEGER NOT NULL, budget INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS saas_grants (
          tenant TEXT NOT NULL REFERENCES saas_accounts(tenant), source TEXT NOT NULL,
          purpose TEXT NOT NULL, valid_from TEXT NOT NULL, expires TEXT NOT NULL,
          evidence TEXT NOT NULL, reviewer TEXT NOT NULL, enabled INTEGER NOT NULL,
          PRIMARY KEY(tenant,source,purpose));
        CREATE TABLE IF NOT EXISTS saas_usage (
          tenant TEXT NOT NULL REFERENCES saas_accounts(tenant), request TEXT NOT NULL,
          fingerprint TEXT NOT NULL, period TEXT NOT NULL, reserved INTEGER NOT NULL,
          actual INTEGER, created TEXT NOT NULL, settled TEXT,
          PRIMARY KEY(tenant,request));
        CREATE TABLE IF NOT EXISTS saas_audit (
          id INTEGER PRIMARY KEY, tenant TEXT NOT NULL, event TEXT NOT NULL,
          body TEXT NOT NULL, at TEXT NOT NULL);
        """)

    def close(self):
        self.db.close()

    @contextmanager
    def _transaction(self):
        self.db.execute("BEGIN" if self.readonly else "BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def _audit(self, tenant, event, body, now):
        self.db.execute("INSERT INTO saas_audit(tenant,event,body,at) VALUES(?,?,?,?)",
                        (tenant, event, json.dumps(body, sort_keys=True), now.isoformat()))

    def configure_account(self, tenant, *, active, monthly_budget_micro_usd, reviewer, now):
        """Operator/billing adapter only; verified webhook state must precede this call."""
        _id(tenant)
        _id(reviewer)
        _amount(monthly_budget_micro_usd)
        if type(active) is not bool:
            raise ValueError("boolean_required")
        now = _time(now)
        with self._transaction():
            self.db.execute("""INSERT INTO saas_accounts VALUES(?,?,?)
              ON CONFLICT(tenant) DO UPDATE SET active=excluded.active,budget=excluded.budget""",
                            (tenant, int(active), monthly_budget_micro_usd))
            self._audit(tenant, "account_configured", {
                "active": active, "budget": monthly_budget_micro_usd, "reviewer": reviewer,
            }, now)

    def grant_data(self, tenant, source, purpose, *, valid_from, expires,
                   evidence_sha256, reviewer, now):
        """Record a human-reviewed licence, not a claim that a licence exists.

        The referenced contract/approval stays in an external private evidence store.
        Each source and use needs its own review; provider authentication is not a grant.
        """
        _id(tenant)
        _id(source)
        _id(reviewer)
        _digest(evidence_sha256)
        if purpose not in PURPOSES:
            raise ValueError("unsupported_data_purpose")
        start, end, now = _time(valid_from), _time(expires), _time(now)
        if end <= start or end <= now:
            raise ValueError("invalid_licence_window")
        with self._transaction():
            self.db.execute("""INSERT INTO saas_grants VALUES(?,?,?,?,?,?,?,1)
              ON CONFLICT(tenant,source,purpose) DO UPDATE SET
              valid_from=excluded.valid_from,expires=excluded.expires,
              evidence=excluded.evidence,reviewer=excluded.reviewer,enabled=1""",
                            (tenant, source, purpose, start.isoformat(), end.isoformat(),
                             evidence_sha256, reviewer))
            self._audit(tenant, "data_granted", {
                "source": source, "purpose": purpose, "evidence": evidence_sha256,
                "reviewer": reviewer, "valid_from": start.isoformat(), "expires": end.isoformat(),
            }, now)

    def revoke_data(self, tenant, source, purpose, *, reviewer, now):
        _id(tenant)
        _id(source)
        _id(reviewer)
        if purpose not in PURPOSES:
            raise ValueError("unsupported_data_purpose")
        now = _time(now)
        with self._transaction():
            self.db.execute("UPDATE saas_grants SET enabled=0 WHERE tenant=? AND source=? AND purpose=?",
                            (tenant, source, purpose))
            self._audit(tenant, "data_revoked", {
                "source": source, "purpose": purpose, "reviewer": reviewer,
            }, now)

    def _admission_reasons(self, tenant, sources, purpose, now):
        account = self.db.execute("SELECT * FROM saas_accounts WHERE tenant=?", (tenant,)).fetchone()
        reasons = []
        if not account or not account["active"]:
            reasons.append("subscription_inactive")
        for source in sources:
            grant = self.db.execute("""SELECT * FROM saas_grants
                WHERE tenant=? AND source=? AND purpose=?""", (tenant, source, purpose)).fetchone()
            if not grant or not grant["enabled"] or not (
                datetime.fromisoformat(grant["valid_from"]) <= now < datetime.fromisoformat(grant["expires"])
            ):
                reasons.append("data_permission_missing:" + source)
        return reasons

    def admission(self, tenant, *, sources, purpose, now):
        """For display/research routes; recheck each request, never cache past expiry."""
        sources, now = self._inputs(tenant, sources, purpose, now)
        with self._transaction():
            reasons = self._admission_reasons(tenant, sources, purpose, now)
        return {"allowed": not reasons, "reasons": reasons}

    @staticmethod
    def _inputs(tenant, sources, purpose, now):
        _id(tenant)
        if purpose not in PURPOSES:
            raise ValueError("unsupported_data_purpose")
        if not isinstance(sources, (list, tuple)) or not sources or len(sources) > 100:
            raise ValueError("source_list_required")
        sources = sorted({_id(s) for s in sources})
        return sources, _time(now)

    def reserve(self, tenant, request, *, input_sha256, model, sources,
                maximum_cost_micro_usd, now):
        """Return execute=True once only, before any model call.

        Bound input hash must cover prompt, model settings and all data. Maximum cost
        needs server-side rate/token bounds. This is a budget check, not a provider bill.
        """
        _id(request)
        _id(model)
        _digest(input_sha256)
        _amount(maximum_cost_micro_usd)
        if not maximum_cost_micro_usd:
            raise ValueError("positive_reservation_required")
        sources, now = self._inputs(tenant, sources, "model_input", now)
        body = [input_sha256, model, sources, maximum_cost_micro_usd]
        fingerprint = hashlib.sha256(json.dumps(body).encode()).hexdigest()
        period = now.strftime("%Y-%m")
        with self._transaction():
            previous = self.db.execute("SELECT * FROM saas_usage WHERE tenant=? AND request=?",
                                       (tenant, request)).fetchone()
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise ValueError("idempotency_conflict")
                return {"execute": False, "reason": "already_reserved_or_settled"}
            reasons = self._admission_reasons(tenant, sources, "model_input", now)
            if reasons:
                self._audit(tenant, "request_denied", {"request": request, "reasons": reasons}, now)
                return {"execute": False, "reason": ";".join(reasons)}
            budget = self.db.execute("SELECT budget FROM saas_accounts WHERE tenant=?",
                                     (tenant,)).fetchone()[0]
            used = self.db.execute("""SELECT COALESCE(SUM(COALESCE(actual,reserved)),0)
                FROM saas_usage WHERE tenant=? AND period=?""", (tenant, period)).fetchone()[0]
            if used + maximum_cost_micro_usd > budget:
                self._audit(tenant, "request_denied", {"request": request, "reasons": ["budget"]}, now)
                return {"execute": False, "reason": "budget_exceeded"}
            self.db.execute("INSERT INTO saas_usage VALUES(?,?,?,?,?,NULL,?,NULL)",
                            (tenant, request, fingerprint, period, maximum_cost_micro_usd, now.isoformat()))
            self._audit(tenant, "cost_reserved", {"request": request, "maximum": maximum_cost_micro_usd}, now)
        return {"execute": True, "reason": "reserved"}

    def settle(self, tenant, request, *, actual_cost_micro_usd, now):
        """Trusted provider receipt only. None keeps the reservation held indefinitely.

        A confirmed zero-cost cancellation may settle zero. A timeout is NOT evidence
        of zero cost. Above-bound actual costs are recorded honestly, never clamped.
        """
        _id(tenant)
        _id(request)
        now = _time(now)
        if actual_cost_micro_usd is not None:
            _amount(actual_cost_micro_usd)
        with self._transaction():
            row = self.db.execute("SELECT * FROM saas_usage WHERE tenant=? AND request=?",
                                  (tenant, request)).fetchone()
            if not row:
                raise ValueError("unknown_request")
            if now < datetime.fromisoformat(row["created"]):
                raise ValueError("settlement_before_request")
            if row["actual"] is not None:
                if row["actual"] != actual_cost_micro_usd:
                    raise ValueError("settlement_conflict")
                return
            if actual_cost_micro_usd is None:
                return
            self.db.execute("UPDATE saas_usage SET actual=?,settled=? WHERE tenant=? AND request=?",
                            (actual_cost_micro_usd, now.isoformat(), tenant, request))
            self._audit(tenant, "cost_settled", {
                "request": request, "actual": actual_cost_micro_usd,
                "exceeded_reservation": actual_cost_micro_usd > row["reserved"],
            }, now)
            if actual_cost_micro_usd > row["reserved"]:
                self.db.execute("UPDATE saas_accounts SET active=0 WHERE tenant=?", (tenant,))
                self._audit(tenant, "account_paused_cost_overrun", {"request": request}, now)

    def usage(self, tenant, *, now):
        _id(tenant)
        now = _time(now)
        period = now.strftime("%Y-%m")
        row = self.db.execute("""SELECT COUNT(*) AS requests,
            COALESCE(SUM(actual),0) AS known_cost,
            COALESCE(SUM(CASE WHEN actual IS NULL THEN reserved ELSE 0 END),0) AS held,
            COALESCE(SUM(actual IS NULL),0) AS unresolved
            FROM saas_usage WHERE tenant=? AND period=?""", (tenant, period)).fetchone()
        return {"period": period, **dict(row), "currency_unit": "micro_usd"}
