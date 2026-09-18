"""Read-only capture verification of explicitly selected operator recovery history.

No credential registry, runtime, audit writer, order transport or recovery operation
is constructed. Historical requests remain historical, including unknown outcomes.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import fields
from datetime import datetime
from pathlib import Path

from quant_ai.execution.audit import XAITrace
from quant_ai.execution.request_context import validate_context
from quant_ai.operations import institutional_recovery as state
from quant_ai.operations.institutional_operator import (
    SCHEMA as AUDIT_SCHEMA,
)
from quant_ai.operations.institutional_operator import SHA, read_audit_records

ROLE = "operator-audit"
SCHEMA = "pramana.operator_audit_capture.v1"
TABLES = {"institutional_operator_meta", "institutional_operator_events"}


def _check(ok, detail):
    if not ok:
        raise ValueError("Operator audit recovery " + detail)


def _raw(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def selection_digest(ledger: Path, oms: Path, accounting: Path, programs: Path) -> str:
    """The operator runtime's declared local storage order; not account authentication."""
    return hashlib.sha256(_raw([str(p.resolve()) for p in (oms, programs, accounting, ledger)]).encode()).hexdigest()


def selection(spec, paths):
    raw = spec["operator_audit"]
    _check("oms" in paths and state.ROLES <= paths.keys(), "requires OMS, accounting and programs")
    _check(type(raw) is str and bool(raw.strip()) and raw == raw.strip() and raw != ":memory:",
           "explicit durable path required")
    path = Path(raw).absolute()
    _check(not path.is_symlink() and path.is_file() and path.stat().st_nlink == 1,
           "unaliased existing audit required")
    _check(path.stat().st_size <= state.MAX_FILE_BYTES, "audit too large")
    return path


def validate_manifest(value, files):
    _check(type(value) is dict and set(value) == {"path", "sourceSelectionSha256", "verification"}
           and value["path"] == ROLE and ROLE in files
           and type(value["sourceSelectionSha256"]) is str and SHA.fullmatch(value["sourceSelectionSha256"])
           and type(value["verification"]) is dict, "inventory invalid")
    meta = files[ROLE]
    _check(type(meta) is dict and set(meta) == {"size", "sha256"}
           and type(meta["size"]) is int and 0 < meta["size"] <= state.MAX_FILE_BYTES,
           "inventory size invalid")


def inspect(audit: Path, ledger: Path, oms: Path, accounting: Path, programs: Path,
            tenant: str, source_selection: str, *, as_of: str) -> dict:
    _check(type(source_selection) is str and SHA.fullmatch(source_selection), "source selection invalid")
    moment = datetime.fromisoformat(as_of)
    _check(moment.tzinfo is not None and moment.utcoffset() is not None, "aware capture timestamp required")
    # Full source verification stays conjunctive; no caller-controlled bypass flag.
    verified = state.inspect(ledger, oms, accounting, programs, tenant)
    with state._database(audit, TABLES) as db, state._database(programs) as schedule, state._database(ledger) as paper:
        meta = db.execute("SELECT schema,tenant,selection FROM institutional_operator_meta").fetchall()
        _check(len(meta) == 1 and tuple(meta[0]) == (AUDIT_SCHEMA, tenant, source_selection),
               "tenant or source selection mismatch")
        records = read_audit_records(db, tenant)
        contexts, latest = {}, {}
        trace_keys = {field.name for field in fields(XAITrace)}
        for record in records:
            instant = datetime.fromisoformat(record["recorded_at"])
            _check(instant <= moment, "audit event is later than capture")
            pid = record["program_id"]
            if pid not in contexts:
                row = schedule.execute("SELECT * FROM execution_programs WHERE program_id=? AND tenant_id=?",
                                       (pid, tenant)).fetchone()
                _check(row is not None, "recorded program missing")
                slices = schedule.execute("SELECT * FROM execution_program_slices WHERE program_id=? ORDER BY sequence", (pid,)).fetchall()
                stored = validate_context(row["context_version"], row["context_payload"],
                    program_id=pid, tenant_id=tenant, parent_payload=row["parent_order_payload"],
                    authority_payload=row["risk_authority_payload"], runtime_digest=row["runtime_context_sha256"],
                    plan_digest=row["plan_sha256"],
                    slices=[(s["sequence"], state._instant(s["scheduled_at"]), s["quantity"]) for s in slices],
                    decision_id=row["decision_id"], created_at=state._instant(row["created_at"]),
                    payload_sha256=row["context_sha256"])
                _check(stored is not None, "recorded context missing")
                provenance = stored.request.proposal.provenance or {}
                source = provenance.get("institutional_source_trace")
                revision = provenance.get("institutional_input_source")
                _check(type(source) is dict and set(source) == trace_keys and source["order_id"] is None
                       and type(revision) is str and bool(revision.strip()), "bridge source missing")
                # Keep every retained receipt, even one committed before programme updates.
                receipts = {}
                for s in slices:
                    evidence = paper.execute("""SELECT e.order_id,e.payload FROM paper_decision_evidence e
                        JOIN paper_ledger l ON e.order_id=l.order_id AND e.tenant_id=l.tenant_id
                        WHERE e.tenant_id=? AND json_extract(e.payload,'$.institutional_program')=?
                        AND json_extract(e.payload,'$.institutional_slice')=?""", (tenant, pid, s["sequence"])).fetchall()
                    _check(len(evidence) <= 1, "ambiguous program receipt")
                    for entry in evidence:
                        payload = state._json(entry["payload"])
                        _check(_raw({key: payload.get(key) for key in trace_keys}) ==
                               _raw({**source, "order_id": entry["order_id"]}), "receipt source trace mismatch")
                        receipts[entry["order_id"]] = s["sequence"]
                contexts[pid] = (row, slices, receipts, hashlib.sha256(revision.encode()).hexdigest())
            row, slices, receipts, revision_sha = contexts[pid]
            _check(record["context_sha256"] == row["context_sha256"], "recorded context mismatch")
            result = record["result"]
            if record["status"] == "RETURNED":
                _check(result["source_revision_sha256"] == revision_sha, "recorded source revision mismatch")
                ids = set(result["committed_order_ids"])
                _check(ids <= receipts.keys(), "recorded receipt missing")
                executed = {s["sequence"] for s in slices if s["state"] == "EXECUTED"}
                _check(set(result["recovered_sequences"]) <= executed
                       and set(result["recovered_sequences"]) <= {receipts[k] for k in ids},
                       "recovered sequence lacks committed evidence")
                complete = result["program_state"] == "COMPLETE" and result["recovery_stage"] == "COMPLETE"
                _check((result["reason_code"] == "recorded_complete") == complete,
                       "inconsistent historical outcome")
                if complete:
                    _check(row["state"] == "COMPLETE" and len(executed) == len(slices)
                           and len(slices) == len(receipts) and ids == receipts.keys(),
                           "historical completion lacks source evidence")
            latest[record["request_id"]] = record
    pending = sum(r["status"] == "REQUESTED" for r in latest.values())
    return {"schema": SCHEMA, "status": "selected_history_verified", "tenant": tenant,
        "events": len(records), "requests": len(latest), "programs": len(contexts),
        "returnedRequests": sum(r["status"] == "RETURNED" for r in latest.values()),
        "failedRequests": sum(r["status"] == "FAILED" for r in latest.values()),
        "requestsWithoutOutcome": pending,
        "returnedReviewRequired": sum(r["status"] == "RETURNED" and r["result"]["reason_code"] == "review_required" for r in latest.values()),
        "historySha256": hashlib.sha256(_raw(records).encode()).hexdigest(),
        "sourceSelectionSha256": source_selection,
        "sourceVerificationSha256": hashlib.sha256(_raw(verified).encode()).hexdigest(),
        "activationAuthorized": False, "recoveryReplayed": False,
        "credentialStoreCaptured": False, "runtimePathRebindRequired": True,
        "scope": "Explicitly selected historical audit and recorded program/receipt correspondence only; unknown outcomes stay unknown. No authentication recreation, recovery replay, path rebind, order submission, halt clearing or risk release."}
