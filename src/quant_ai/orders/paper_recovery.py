"""Explicit recovery of committed bound-cash swarm fills into the local OMS.

No submit/cancel/reject operation, ledger mutation, accounting posting, or halt clearing.
A reviewed snapshot is a consistency boundary, not authenticated operator identity.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from quant_ai.domain.models import AssetClass, Market
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.governance.runtime_identity import path_digest, runtime_identity_configuration
from quant_ai.instruments.identity import canonical_instrument_identity
from quant_ai.operations.idempotency import order_idempotency_key
from quant_ai.orders.intent import canonical_order_intent
from quant_ai.orders.oms import PAPER_RECOVERY_SCHEMA_VERSION, DurableOms
from quant_ai.orders.state import OrderState

SCHEMA = "pramana.paper_oms_recovery.v1"
TABLE = "oms_paper_recoveries"


def _encoded(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _text(value, name):
    if (not isinstance(value, str) or not 0 < len(value) <= 256
            or value != value.strip() or any(not c.isprintable() for c in value)):
        raise ValueError(f"paper_recovery_{name}_invalid")


def _now(value):
    moment = datetime.now(timezone.utc) if value is None else value
    if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("paper_recovery_time_must_be_aware")
    return moment.astimezone(timezone.utc)


@dataclass(frozen=True)
class PaperOmsRecoveryPlan:
    status: str
    reason: str
    payload: str

    @property
    def sha256(self) -> str:
        return _hash(self.payload)


class PaperOmsRecovery:
    def __init__(self, *, broker: PaperBrokerService, oms: DurableOms):
        if type(broker) is not PaperBrokerService or type(oms) is not DurableOms:
            raise TypeError("paper_recovery_local_paper_components_required")
        self.broker, self.oms = broker, oms

    def _idle(self):
        if self.broker._connection.in_transaction or self.oms.db.in_transaction:
            raise ValueError("paper_recovery_outer_transaction_not_supported")

    def inspect(self, client_order_id: str, *, tenant_id: str, now=None) -> PaperOmsRecoveryPlan:
        """Inspect only; no schema changes, recovery writes, or account-level ready claim."""
        _text(tenant_id, "tenant")
        moment = None if now is None else _now(now)
        with self.broker._lock, self.oms._lock:
            self._idle()
            with self.oms.transaction(), self.broker.accounting_read_snapshot():
                # Implicit time describes this serialized read, not time spent waiting
                # behind another writer. An explicitly supplied historical time stays fixed.
                moment = _now(None) if moment is None else moment
                return self._inspect(client_order_id, tenant_id, moment)[0]

    def _inspect(self, client_id, tenant, moment):
        payload = {"schema": SCHEMA, "client_order_id": client_id, "tenant_id": tenant}
        try:
            current = self.oms.get(client_id)
            if current.tenant_id != tenant:
                raise ValueError("paper_recovery_tenant_mismatch")
            verification = self.oms.verify(client_id)
            order = self.oms.get_intent(client_id)
            instrument = getattr(order, "instrument", None)
            if (instrument is None or order.market is not Market.INDIA
                    or order.asset_class not in {AssetClass.EQUITY, AssetClass.ETF}
                    or instrument.currency != "INR" or instrument.exchange != "NSE"):
                raise ValueError("paper_recovery_bound_cash_scope_required")
            configuration = runtime_identity_configuration(self.broker, tenant)
            if (configuration is None
                    or configuration["oms_path_sha256"] != path_digest(self.oms.path)
                    or configuration["instruments"].get(order.symbol)
                    != canonical_instrument_identity(instrument)):
                raise ValueError("paper_recovery_configuration_mismatch")
            if self.oms.broker_evidence_binding(client_id) is not None:
                raise ValueError("paper_recovery_external_order_not_supported")
            creation = json.loads(self.oms.db.execute(
                "SELECT payload FROM oms_events WHERE client_order_id=? AND sequence=1", (client_id,),
            ).fetchone()[0])
            decision = creation["decisionId"]
            if self.oms.client_order_id(order, decision) != client_id:
                raise ValueError("paper_recovery_decision_identity_mismatch")
            key = order_idempotency_key(order, decision).value
            receipt = self.broker.submission_receipt(key, tenant)
            if receipt is None:
                raise ValueError("paper_recovery_receipt_unavailable")
            entry, evidence = receipt.entry, receipt.evidence
            if (canonical_order_intent(receipt.order) != canonical_order_intent(order)
                    or evidence.get("schema") != "pramana.swarm_fill.v1"
                    or evidence.get("event_type") != "swarm_fill"
                    or evidence.get("decision_id") != decision
                    or evidence.get("risk_verdict", {}).get("approved") != "true"):
                raise ValueError("paper_recovery_receipt_intent_or_decision_mismatch")
            binding = (evidence.get("provenance") or {}).get("runtime_strategy")
            if not isinstance(binding, dict) or binding.get("status") != "matched":
                raise ValueError("paper_recovery_release_evidence_unverified")
            stored = self.broker._connection.execute(
                "SELECT payload FROM pilot_strategy_manifests WHERE tenant_id=? AND sha256=?",
                (tenant, binding.get("sha256")),
            ).fetchone()
            if stored is None or _hash(stored[0]) != binding.get("sha256"):
                raise ValueError("paper_recovery_release_manifest_mismatch")
            manifest = json.loads(stored[0])
            if (manifest.get("tenant_id") != tenant or manifest.get("execution_mode") != "paper"
                    or manifest.get("order_identity") != configuration):
                raise ValueError("paper_recovery_release_scope_mismatch")
            if (entry.created_at.tzinfo is None or entry.created_at < current.created_at
                    or entry.created_at > moment or current.updated_at > moment):
                raise ValueError("paper_recovery_execution_time_invalid")
            conflicts = self.oms.db.execute(
                "SELECT client_order_id FROM oms_orders WHERE broker_order_id=? AND client_order_id<>?",
                (entry.order_id, client_id),
            ).fetchall()
            claimed = self.oms.db.execute(
                "SELECT client_order_id FROM oms_fills WHERE fill_id=? AND client_order_id<>?",
                (entry.order_id, client_id),
            ).fetchone()
            if conflicts or claimed is not None or current.broker_order_id not in {None, entry.order_id}:
                raise ValueError("paper_recovery_broker_order_conflict")
            payload.update({
                "intent": canonical_order_intent(order), "decision_id": decision,
                "idempotency_key": key, "configuration_sha256": _hash(_encoded(configuration)),
                "receipt_sha256": _hash(_encoded(evidence)), "paper_order_id": entry.order_id,
                "quantity": entry.quantity, "price": str(entry.fill_price),
                "executed_at": entry.created_at.astimezone(timezone.utc).isoformat(),
                "oms_head": verification["headHash"], "oms_state": current.state.value,
            })
            fills = self.oms.fill_ids(client_id)
            if current.state is OrderState.FILLED:
                if (fills != (entry.order_id,) or current.filled_quantity != entry.quantity
                        or current.average_fill_price != entry.fill_price
                        or current.broker_order_id != entry.order_id):
                    raise ValueError("paper_recovery_completed_fill_mismatch")
                status, reason = "MATCHED", "paper_recovery_fill_already_matched"
            elif current.state in {OrderState.SUBMITTED, OrderState.SUBMISSION_UNCERTAIN}:
                if fills or current.filled_quantity != 0:
                    raise ValueError("paper_recovery_partial_fill_requires_review")
                status, reason = "READY", "paper_recovery_committed_fill_available"
            else:
                raise ValueError("paper_recovery_order_state_requires_review")
            record = self._recorded(client_id)
            markers = [json.loads(row[0])["paperRecovery"] for row in self.oms.db.execute(
                "SELECT payload FROM oms_events WHERE client_order_id=? AND kind='FILL'", (client_id,),
            ) if "paperRecovery" in json.loads(row[0])]
            if record is None and markers:
                raise ValueError("paper_recovery_audit_missing")
            if record is not None:
                if _now(datetime.fromisoformat(record["recovered_at"])) > moment:
                    raise ValueError("paper_recovery_audit_from_future")
                expected_marker = {
                    "schema": SCHEMA, "plan_sha256": record["plan_sha256"],
                    "receipt_sha256": record["plan"]["receipt_sha256"],
                    "reviewer": record["reviewer"], "recovered_at": record["recovered_at"],
                }
                if markers != [expected_marker]:
                    raise ValueError("paper_recovery_audit_mismatch")
                plan = record["plan"]
                if (status != "MATCHED" or record["after_oms_head"] != verification["headHash"]
                        or record["plan_sha256"] != _hash(_encoded(plan))
                        or any(plan.get(k) != payload[k] for k in payload if k not in {"oms_head", "oms_state"})):
                    raise ValueError("paper_recovery_audit_mismatch")
            return PaperOmsRecoveryPlan(status, reason, _encoded(payload)), receipt, record
        except (ValueError, TypeError, KeyError, AttributeError, IndexError, sqlite3.Error) as error:
            # Do not export raw SQLite/provider messages or possibly sensitive payloads.
            reason = str(error) if isinstance(error, ValueError) and str(error).startswith("paper_recovery_") else "paper_recovery_evidence_invalid"
            if isinstance(error, ValueError) and str(error) in {
                "oms_paper_recovery_audit_missing", "oms_paper_recovery_audit_mismatch",
            }:
                reason = str(error).removeprefix("oms_")
            payload["blocked_reason"] = reason
            return PaperOmsRecoveryPlan("BLOCKED", reason, _encoded(payload)), None, None

    def _recorded(self, client_id):
        if not self.oms.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
            return None
        row = self.oms.db.execute("SELECT payload,sha256,tenant_id FROM oms_paper_recoveries WHERE client_order_id=?", (client_id,)).fetchone()
        if row is None:
            return None
        if _hash(row[0]) != row[1]:
            raise ValueError("paper_recovery_audit_mismatch")
        record = json.loads(row[0])
        if (set(record) != {"schema", "plan", "plan_sha256", "reviewer", "recovered_at", "after_oms_head"}
                or record["schema"] != SCHEMA):
            raise ValueError("paper_recovery_audit_mismatch")
        if (not isinstance(record["plan"], dict)
                or record["plan"].get("client_order_id") != client_id
                or record["plan"].get("tenant_id") != row[2]):
            raise ValueError("paper_recovery_audit_mismatch")
        _text(record["reviewer"], "reviewer")
        recovered = _now(datetime.fromisoformat(record["recovered_at"]))
        if recovered < _now(datetime.fromisoformat(record["plan"]["executed_at"])):
            raise ValueError("paper_recovery_audit_time_invalid")
        return record

    def recover(self, client_order_id: str, *, tenant_id: str, reviewed_plan_sha256: str,
                reviewer: str, now=None) -> PaperOmsRecoveryPlan:
        """Mirror one already committed fill after exact review; keep the durable halt."""
        _text(tenant_id, "tenant")
        _text(reviewer, "reviewer")
        if not isinstance(reviewed_plan_sha256, str) or not re.fullmatch("[0-9a-f]{64}", reviewed_plan_sha256):
            raise ValueError("paper_recovery_reviewed_plan_invalid")
        moment = None if now is None else _now(now)
        with self.broker._lock, self.oms._lock:
            self._idle()
            with self.oms.transaction(), self.broker.accounting_read_snapshot():
                # Implicit time describes this serialized read, not time spent waiting
                # behind another writer. An explicitly supplied historical time stays fixed.
                moment = _now(None) if moment is None else moment
                halt = self.broker._connection.execute(
                    "SELECT kill_switch_engaged FROM risk_control_state WHERE tenant_id=?", (tenant_id,),
                ).fetchone()
                if halt is None or halt[0] != 1:
                    raise ValueError("paper_recovery_durable_halt_required")
                plan, receipt, record = self._inspect(client_order_id, tenant_id, moment)
                if plan.status == "BLOCKED":
                    raise ValueError(plan.reason)
                if record is not None:
                    if record["plan_sha256"] != reviewed_plan_sha256:
                        raise ValueError("paper_recovery_reviewed_plan_changed")
                    return plan
                if plan.sha256 != reviewed_plan_sha256:
                    raise ValueError("paper_recovery_reviewed_plan_changed")
                if plan.status == "MATCHED":
                    return plan
                assert receipt is not None
                entry = receipt.entry
                self.oms.fill(client_order_id, fill_id=entry.order_id, quantity=entry.quantity,
                              price=entry.fill_price, broker_order_id=entry.order_id, now=entry.created_at,
                              paper_recovery={
                                  "schema": SCHEMA, "plan_sha256": plan.sha256,
                                  "receipt_sha256": json.loads(plan.payload)["receipt_sha256"],
                                  "reviewer": reviewer, "recovered_at": moment.isoformat(),
                              })
                after_head = self.oms.db.execute(
                    "SELECT event_hash FROM oms_events WHERE client_order_id=? ORDER BY sequence DESC LIMIT 1",
                    (client_order_id,),
                ).fetchone()[0]
                self._append_record(client_order_id, tenant_id, {
                    "schema": SCHEMA, "plan": json.loads(plan.payload), "plan_sha256": plan.sha256,
                    "reviewer": reviewer, "recovered_at": moment.isoformat(),
                    "after_oms_head": after_head,
                })
                verified = self._inspect(client_order_id, tenant_id, moment)[0]
                if verified.status != "MATCHED":
                    raise ValueError(verified.reason)
                return verified

    def _append_record(self, client_id, tenant, payload):
        self.oms.db.execute("CREATE TABLE IF NOT EXISTS oms_paper_recoveries (client_order_id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL)")
        for verb in ("UPDATE", "DELETE"):
            self.oms.db.execute(f"CREATE TRIGGER IF NOT EXISTS oms_paper_recoveries_{verb.lower()}_blocked BEFORE {verb} ON oms_paper_recoveries BEGIN SELECT RAISE(ABORT,'Paper OMS recovery history is append-only'); END")
        raw = _encoded(payload)
        self.oms.db.execute("INSERT INTO oms_paper_recoveries VALUES (?,?,?,?)", (client_id, tenant, raw, _hash(raw)))
        # Old v1-only readers must refuse a recovered journal on restart. The schema
        # flag and proof commit with the fill; a failed attempt keeps the old version.
        self.oms.db.execute("UPDATE oms_meta SET version=? WHERE id=1", (PAPER_RECOVERY_SCHEMA_VERSION,))
