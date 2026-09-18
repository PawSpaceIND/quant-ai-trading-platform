"""Permission-scoped historical paper reconciliation with a write-ahead audit.

Server code selects the runtime and audit file. Callers cannot supply tenant/storage
or provider configuration. The operation cannot submit orders or release risk.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

from quant_ai.agents.institutional_runtime import (
    InstitutionalRecoveryReport,
    InstitutionalSwarmPaperTradingService,
)
from quant_ai.security.api_keys import ApiCredential

SCHEMA = "pramana.institutional_operator_audit.v1"
READ = "institutional.recovery.read"
APPLY = "institutional.recovery.apply"
CONFIRM = "reconcile_recorded_paper_bookkeeping_only"
IDENTIFIER = re.compile(r"[A-Za-z0-9._:-]{1,180}")
SHA = re.compile(r"[0-9a-f]{64}")
MAX_EVENTS = 10000
MAX_EVENT_BYTES = 65536


class OperatorRecoveryError(ValueError):
    def __init__(self, status_code: int, code: str):
        super().__init__(code)
        self.status_code = status_code
        self.code = code


def _raw(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def _text(value):
    if type(value) is not str or not IDENTIFIER.fullmatch(value):
        raise OperatorRecoveryError(400, "invalid_recovery_identifier")


def _audit_check(value):
    if not value:
        raise OperatorRecoveryError(503, "institutional_recovery_audit_unavailable")


def _unique(pairs):
    result = {}
    for key, value in pairs:
        _audit_check(key not in result)
        result[key] = value
    return result


def validate_audit_result(result, program_id, tenant_id):
    _audit_check(set(result) == {"program_id", "tenant_id", "program_state", "recovery_stage",
                "committed_order_ids", "recovered_sequences", "source_revision_sha256",
                "reason_code", "execution_authorized"}
        and result["execution_authorized"] is False
        and result["program_id"] == program_id and result["tenant_id"] == tenant_id
        and result["program_state"] in {"PLANNED", "ACTIVE", "COMPLETE", "FAILED", "CANCELLED"}
        and result["recovery_stage"] in {"READY", "COMPLETE", "FAILED", "RECOVERY_REQUIRED"}
        and result["reason_code"] in {"recorded_complete", "review_required"}
        and type(result["source_revision_sha256"]) is str
        and SHA.fullmatch(result["source_revision_sha256"])
        and isinstance(result["committed_order_ids"], (list, tuple))
        and len(result["committed_order_ids"]) <= 1
        and all(type(v) is str and IDENTIFIER.fullmatch(v) for v in result["committed_order_ids"])
        and isinstance(result["recovered_sequences"], (list, tuple))
        and len(result["recovered_sequences"]) <= 1
        and all(type(v) is int and v == 1 for v in result["recovered_sequences"]))


def read_audit_records(db, tenant_id):
    """Replay the bounded persisted audit without a runtime or any writes."""
    rows = db.execute("SELECT sequence,payload,sha256 FROM institutional_operator_events ORDER BY sequence LIMIT ?",
                           (MAX_EVENTS + 1,)).fetchall()
    _audit_check(len(rows) <= MAX_EVENTS)
    records, requests, previous = [], {}, "GENESIS"
    for number, row in enumerate(rows, 1):
        _audit_check(type(row[0]) is int and row[0] == number and len(row[1].encode()) <= MAX_EVENT_BYTES)
        value = json.loads(row[1], object_pairs_hook=_unique)
        _audit_check(type(value) is dict and set(value) == {
            "schema", "sequence", "tenant", "request_id", "actor_key_id", "program_id",
            "context_sha256", "status", "recorded_at", "result", "previous_sha256"})
        _audit_check(_raw(value) == row[1] and _sha(row[1]) == row[2]
                     and value["schema"] == SCHEMA and type(value["sequence"]) is int
                     and value["sequence"] == number and value["tenant"] == tenant_id
                     and value["previous_sha256"] == previous)
        for field in ("request_id", "actor_key_id", "program_id"):
            _audit_check(type(value[field]) is str and IDENTIFIER.fullmatch(value[field]))
        _audit_check(type(value["context_sha256"]) is str and SHA.fullmatch(value["context_sha256"]))
        instant = datetime.fromisoformat(value["recorded_at"])
        _audit_check(instant.tzinfo is not None and instant.utcoffset() is not None)
        key = value["request_id"]
        older = requests.get(key)
        if value["status"] == "REQUESTED":
            _audit_check(older is None and value["result"] is None)
        else:
            _audit_check(older is not None and older["status"] == "REQUESTED"
                         and value["status"] in {"RETURNED", "FAILED"}
                         and all(value[k] == older[k] for k in (
                             "actor_key_id", "program_id", "context_sha256")))
            _audit_check(type(value["result"]) is dict)
            if value["status"] == "RETURNED":
                validate_audit_result(value["result"], value["program_id"], tenant_id)
            else:
                _audit_check(value["result"] == {"code": "institutional_reconciliation_requires_review"})
        records.append(value)
        requests[key] = value
        previous = row[2]
    return records


class InstitutionalRecoveryOperations:
    """Internal authenticated-handler component; not an order or automatic recovery service."""

    def __init__(self, runtime: InstitutionalSwarmPaperTradingService, audit_path: str | Path):
        if type(runtime) is not InstitutionalSwarmPaperTradingService:
            raise ValueError("institutional_recovery_runtime_required")
        self.runtime = runtime
        self._runtime_selection = (runtime, runtime.broker, runtime.oms, runtime._inputs.programs,
                                   runtime._inputs.accounting, runtime._inputs.accounting.journal)
        self.tenant_id = runtime._inputs.accounting.tenant_id
        _text(self.tenant_id)
        raw_path = str(audit_path)
        if not raw_path.strip() or raw_path == ":memory:":
            raise ValueError("institutional_recovery_durable_audit_required")
        self.path = Path(audit_path).absolute()
        sources = [runtime.oms.path, runtime._inputs.programs.path,
                   runtime._inputs.accounting.journal.path]
        sources += [Path(r[2]) for r in runtime.broker._connection.execute("PRAGMA database_list")
                    if r[1] == "main" and r[2]]
        if (self.path.is_symlink() or self.path.is_dir()
                or self.path.resolve() in {Path(p).resolve() for p in sources}
                or self.path.exists() and self.path.stat().st_nlink != 1):
            raise ValueError("institutional_recovery_distinct_audit_required")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        created = False
        if not self.path.exists():
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                os.close(fd)
                created = True
            except FileExistsError:
                pass
        self._file_identity = (self.path.stat().st_dev, self.path.stat().st_ino)
        self._lock = RLock()
        self._selection = _sha(_raw([str(Path(p).resolve()) for p in sources]))
        self.db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True,
                                  check_same_thread=False, timeout=5)
        self.db.row_factory = sqlite3.Row
        try:
            if created:
                with self.db:
                    self.db.execute("CREATE TABLE institutional_operator_meta(schema TEXT,tenant TEXT,selection TEXT)")
                    self.db.execute("INSERT INTO institutional_operator_meta VALUES(?,?,?)",
                                    (SCHEMA, self.tenant_id, self._selection))
                    self.db.execute("""CREATE TABLE institutional_operator_events(
                        sequence INTEGER PRIMARY KEY, payload TEXT NOT NULL, sha256 TEXT NOT NULL)""")
                    for table in ("institutional_operator_meta", "institutional_operator_events"):
                        for verb in ("UPDATE", "DELETE"):
                            self.db.execute(f"""CREATE TRIGGER {table}_{verb.lower()}_blocked
                                BEFORE {verb} ON {table}
                                BEGIN SELECT RAISE(ABORT,'Operator recovery audit is append-only'); END""")
            self._check_storage()
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self._records()
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def _check_storage(self):
        _audit_check(not self.path.is_symlink() and self.path.is_file()
                     and self.path.stat().st_nlink == 1
                     and (self.path.stat().st_dev, self.path.stat().st_ino) == self._file_identity)
        rows = self.db.execute("SELECT schema,tenant,selection FROM institutional_operator_meta").fetchall()
        _audit_check(len(rows) == 1 and tuple(rows[0]) == (SCHEMA, self.tenant_id, self._selection))

    @contextmanager
    def _transaction(self):
        with self._lock:
            try:
                self._check_storage()
                self.db.execute("BEGIN IMMEDIATE")
                yield
                self.db.commit()
            except BaseException:
                if self.db.in_transaction:
                    self.db.rollback()
                raise

    def _authorize(self, credential, permission):
        if not isinstance(credential, ApiCredential) or permission not in credential.scopes:
            raise OperatorRecoveryError(403, "institutional_recovery_permission_required")
        if credential.tenant_id != self.tenant_id:
            raise OperatorRecoveryError(404, "institutional_recovery_target_unavailable")
        live = self.runtime
        selection = (live, live.broker, live.oms, live._inputs.programs,
                     live._inputs.accounting, live._inputs.accounting.journal)
        if (any(selected is not current for selected, current in zip(self._runtime_selection, selection, strict=True))
                or live._inputs.accounting.tenant_id != self.tenant_id):
            raise OperatorRecoveryError(503, "institutional_recovery_runtime_unavailable")

    def _records(self):
        self._check_storage()
        return read_audit_records(self.db, self.tenant_id)

    def _append(self, records, *, request_id, actor_key_id, program_id, context_sha256, status, result):
        _audit_check(len(records) < MAX_EVENTS)
        value = {"schema": SCHEMA, "sequence": len(records) + 1, "tenant": self.tenant_id,
                 "request_id": request_id, "actor_key_id": actor_key_id, "program_id": program_id,
                 "context_sha256": context_sha256, "status": status,
                 "recorded_at": datetime.now(timezone.utc).isoformat(), "result": result,
                 "previous_sha256": _sha(_raw(records[-1])) if records else "GENESIS"}
        raw = _raw(value)
        _audit_check(len(raw.encode()) <= MAX_EVENT_BYTES)
        self.db.execute("INSERT INTO institutional_operator_events VALUES(?,?,?)",
                        (value["sequence"], raw, _sha(raw)))
        return value

    def _validate_result(self, result, program_id):
        return validate_audit_result(result, program_id, self.tenant_id)

    def _safe_result(self, report, program_id):
        _audit_check(type(report) is InstitutionalRecoveryReport
                     and type(report.source_revision) is str and 0 < len(report.source_revision) <= 180)
        result = {"program_id": report.program_id, "tenant_id": report.tenant_id,
                  "program_state": report.program_state, "recovery_stage": report.recovery_stage,
                  "committed_order_ids": report.committed_order_ids,
                  "recovered_sequences": report.recovered_sequences,
                  "source_revision_sha256": _sha(report.source_revision),
                  "reason_code": ("recorded_complete" if report.program_state == "COMPLETE"
                                  and report.recovery_stage == "COMPLETE" else "review_required"),
                  "execution_authorized": report.execution_authorized}
        self._validate_result(result, program_id)
        return result

    @staticmethod
    def _response(record, *, replayed=False):
        return {"tenant_id": record["tenant"], "request_id": record["request_id"], "program_id": record["program_id"],
                "actor_key_id": record["actor_key_id"], "context_sha256": record["context_sha256"],
                "status": record["status"], "recorded_at": record["recorded_at"],
                "result": record["result"], "replayed": replayed, "execution_authorized": False}

    def _context(self, program_id):
        _text(program_id)
        try:
            stored = self.runtime._inputs.programs.load_context(program_id, tenant_id=self.tenant_id)
            program = self.runtime._inputs.programs.get(program_id)
        except KeyError as error:
            raise OperatorRecoveryError(404, "institutional_recovery_target_unavailable") from error
        if type(program.context_sha256) is not str or not SHA.fullmatch(program.context_sha256):
            raise OperatorRecoveryError(409, "institutional_recovery_context_unavailable")
        revision = (stored.request.proposal.provenance or {}).get("institutional_input_source")
        if type(revision) is not str or not revision.strip() or len(revision) > 180:
            raise OperatorRecoveryError(409, "institutional_recovery_context_unavailable")
        return {"tenant_id": self.tenant_id, "program_id": program_id, "context_sha256": program.context_sha256,
                "program_state": program.state.value, "slice_states": [s.state.value for s in program.slices],
                "source_revision_sha256": _sha(revision), "execution_authorized": False,
                "confirmation_required": CONFIRM,
                "scope": "Original paper programme and recorded tenant protective-exit bookkeeping; no new orders, retries, risk release or halt clearing"}

    def inspect_program(self, program_id, credential):
        self._authorize(credential, READ)
        with self.runtime._route_lock:
            self._authorize(credential, READ)
            return self._context(program_id)

    def request_status(self, request_id, credential):
        self._authorize(credential, READ)
        _text(request_id)
        with self._lock:
            records = self._records()
        latest = next((r for r in reversed(records) if r["request_id"] == request_id), None)
        if latest is None:
            raise OperatorRecoveryError(404, "institutional_recovery_request_unavailable")
        return self._response(latest)

    def apply(self, program_id, *, request_id, context_sha256, confirmation, credential):
        self._authorize(credential, APPLY)
        _text(program_id)
        _text(request_id)
        if (type(context_sha256) is not str or not SHA.fullmatch(context_sha256)
                or confirmation != CONFIRM or type(confirmation) is not str):
            raise OperatorRecoveryError(400, "institutional_recovery_confirmation_required")
        with self.runtime._route_lock:
            self._authorize(credential, APPLY)
            with self._transaction():
                records = self._records()
                previous = next((r for r in reversed(records) if r["request_id"] == request_id), None)
                if previous is not None:
                    if (previous["actor_key_id"], previous["program_id"], previous["context_sha256"]) != (
                            credential.key_id, program_id, context_sha256):
                        raise OperatorRecoveryError(409, "institutional_recovery_request_conflict")
                    if previous["status"] != "RETURNED":
                        raise OperatorRecoveryError(409, "institutional_recovery_previous_outcome_requires_review")
                    return self._response(previous, replayed=True)
                context = self._context(program_id)
                if context["context_sha256"] != context_sha256:
                    raise OperatorRecoveryError(409, "institutional_recovery_context_changed")
                # Reserve room for the outcome before any source state can change.
                _audit_check(len(records) <= MAX_EVENTS - 2)
                self._append(records, request_id=request_id, actor_key_id=credential.key_id,
                             program_id=program_id, context_sha256=context_sha256,
                             status="REQUESTED", result=None)
            try:
                report = self.runtime.reconcile_program(program_id, tenant_id=self.tenant_id)
                result = self._safe_result(report, program_id)
            except (ValueError, TypeError, KeyError, OSError, RuntimeError, ArithmeticError, sqlite3.Error):
                self._finish(request_id, "FAILED", {"code": "institutional_reconciliation_requires_review"})
                raise OperatorRecoveryError(409, "institutional_reconciliation_requires_review") from None
            terminal = self._finish(request_id, "RETURNED", result)
            return self._response(terminal)

    def _finish(self, request_id, status, result):
        with self._transaction():
            records = self._records()
            previous = next((r for r in reversed(records) if r["request_id"] == request_id), None)
            _audit_check(previous is not None and previous["status"] == "REQUESTED")
            return self._append(records, request_id=request_id, actor_key_id=previous["actor_key_id"],
                                program_id=previous["program_id"], context_sha256=previous["context_sha256"],
                                status=status, result=result)
