"""Durable, broker-neutral order-management journal.

This module records order intent and lifecycle; it never sends a broker request.  The event
journal is append-only and hash chained, while ``oms_orders`` is a restart-friendly projection
that is independently checked against the event stream before it is trusted.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Self

from quant_ai.domain.models import OrderIntent
from quant_ai.orders.state import OrderLifecycle, OrderState

SCHEMA_VERSION = 1
_SHA = re.compile(r"[0-9a-f]{64}")
_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")
TERMINAL = frozenset({OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED})


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _instant(value: datetime, name: str) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name}_must_be_timezone_aware")
    return value.astimezone(timezone.utc).isoformat()


def _decimal(value: object, name: str, *, positive: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError(f"invalid_{name}") from error
    if not result.is_finite() or (positive and result <= 0):
        raise ValueError(f"invalid_{name}")
    return result


@dataclass(frozen=True)
class OmsOrder:
    client_order_id: str
    tenant_id: str
    strategy_id: str
    state: OrderState
    symbol: str
    market: str
    asset_class: str
    side: str
    requested_quantity: int
    reference_price: Decimal
    filled_quantity: int
    average_fill_price: Decimal | None
    broker_order_id: str | None
    created_at: datetime
    updated_at: datetime
    last_event_sequence: int

    @property
    def pending_quantity(self) -> int:
        return self.requested_quantity - self.filled_quantity


class DurableOms:
    """SQLite OMS projection plus append-only event/fill history."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if self.path != Path(":memory:") and self.path.exists() and self.path.is_symlink():
            raise ValueError("oms_symlink_unsupported")
        if str(self.path) != ":memory:" and not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        self.db = sqlite3.connect(str(self.path), timeout=10, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=2000")
        self._schema()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def _schema(self) -> None:
        with self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS oms_meta(
                    id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oms_orders(
                    client_order_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    strategy_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    market TEXT NOT NULL,
                    asset_class TEXT NOT NULL,
                    side TEXT NOT NULL,
                    requested_quantity INTEGER NOT NULL,
                    reference_price TEXT NOT NULL,
                    filled_quantity INTEGER NOT NULL,
                    average_fill_price TEXT,
                    broker_order_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_event_sequence INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oms_events(
                    client_order_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    at TEXT NOT NULL,
                    previous_hash TEXT,
                    event_hash TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY(client_order_id, sequence),
                    UNIQUE(event_hash)
                );
                CREATE TABLE IF NOT EXISTS oms_fills(
                    fill_id TEXT PRIMARY KEY,
                    client_order_id TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    price TEXT NOT NULL,
                    at TEXT NOT NULL,
                    broker_order_id TEXT
                );
                CREATE TABLE IF NOT EXISTS oms_replacements(
                    original_client_order_id TEXT PRIMARY KEY,
                    replacement_client_order_id TEXT NOT NULL UNIQUE,
                    reason TEXT NOT NULL,
                    at TEXT NOT NULL
                );
            """)
            row = self.db.execute("SELECT version FROM oms_meta WHERE id=1").fetchone()
            if row is None:
                self.db.execute("INSERT INTO oms_meta VALUES(1,?)", (SCHEMA_VERSION,))
            elif row[0] != SCHEMA_VERSION:
                raise ValueError("oms_schema_version_mismatch")
            for table in ("oms_events", "oms_fills", "oms_replacements"):
                for verb in ("UPDATE", "DELETE"):
                    name = f"{table}_{verb.lower()}_blocked"
                    self.db.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {name} BEFORE {verb} ON {table} "
                        "BEGIN SELECT RAISE(ABORT,'OMS history is append-only'); END"
                    )

    @staticmethod
    def client_order_id(order: OrderIntent, decision_id: str) -> str:
        if not decision_id.strip():
            raise ValueError("decision_id_required")
        raw = {
            "tenant": order.tenant_id,
            "strategy": order.strategy_id,
            "market": order.market.value,
            "assetClass": order.asset_class.value,
            "symbol": order.symbol,
            "side": order.side.value,
            "quantity": order.quantity,
            "referencePrice": str(order.reference_price),
            "decisionId": decision_id,
        }
        return "OMS-" + _hash(raw)[:40]

    def create(
        self,
        order: OrderIntent,
        *,
        decision_id: str,
        now: datetime | None = None,
    ) -> OmsOrder:
        if type(order.quantity) is not int or order.quantity <= 0 or order.reference_price <= 0:
            raise ValueError("oms_invalid_order_geometry")
        at = now or datetime.now(timezone.utc)
        stamp = _instant(at, "order_created_at")
        client_id = self.client_order_id(order, decision_id)
        with self.db:
            existing = self.db.execute(
                "SELECT * FROM oms_orders WHERE client_order_id=?", (client_id,)
            ).fetchone()
            if existing is not None:
                decoded = self._decode(existing)
                self._assert_same_intent(decoded, order)
                return decoded
            self.db.execute(
                """INSERT INTO oms_orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (client_id, order.tenant_id, order.strategy_id, OrderState.CREATED.value,
                 order.symbol, order.market.value, order.asset_class.value, order.side.value,
                 order.quantity, str(order.reference_price), 0, None, None, stamp, stamp, 0),
            )
            self._append_event_locked(client_id, "CREATED", at, {
                "decisionId": decision_id,
                "order": {
                    "tenantId": order.tenant_id, "strategyId": order.strategy_id,
                    "market": order.market.value, "assetClass": order.asset_class.value,
                    "symbol": order.symbol, "side": order.side.value,
                    "quantity": order.quantity, "referencePrice": str(order.reference_price),
                },
            })
        return self.get(client_id)

    @staticmethod
    def _assert_same_intent(current: OmsOrder, order: OrderIntent) -> None:
        if (
            current.tenant_id != order.tenant_id
            or current.strategy_id != order.strategy_id
            or current.symbol != order.symbol
            or current.market != order.market.value
            or current.asset_class != order.asset_class.value
            or current.side != order.side.value
            or current.requested_quantity != order.quantity
            or current.reference_price != order.reference_price
        ):
            raise ValueError("client_order_id_intent_mismatch")

    def transition(
        self,
        client_order_id: str,
        target: OrderState,
        *,
        reason: str | None = None,
        broker_order_id: str | None = None,
        now: datetime | None = None,
    ) -> OmsOrder:
        self._validate_id(client_order_id, "client_order_id")
        if broker_order_id is not None:
            self._validate_id(broker_order_id, "broker_order_id")
        at = now or datetime.now(timezone.utc)
        with self.db:
            row = self.db.execute(
                "SELECT * FROM oms_orders WHERE client_order_id=?", (client_order_id,)
            ).fetchone()
            if row is None:
                raise KeyError(client_order_id)
            current = self._decode(row)
            lifecycle = OrderLifecycle(current.state)
            lifecycle.transition(target)
            broker = self._broker_identity(current.broker_order_id, broker_order_id)
            self.db.execute(
                "UPDATE oms_orders SET state=?,broker_order_id=?,updated_at=? WHERE client_order_id=?",
                (target.value, broker, _instant(at, "order_transition_at"), client_order_id),
            )
            self._append_event_locked(client_order_id, target.value, at, {
                "from": current.state.value, "to": target.value,
                "reason": (reason or "").strip() or None, "brokerOrderId": broker,
            })
        return self.get(client_order_id)

    def approve_risk(self, client_order_id: str, *, now: datetime | None = None) -> OmsOrder:
        return self.transition(client_order_id, OrderState.RISK_APPROVED, now=now)

    def submitted(
        self, client_order_id: str, *, broker_order_id: str | None = None,
        now: datetime | None = None,
    ) -> OmsOrder:
        return self.transition(
            client_order_id, OrderState.SUBMITTED, broker_order_id=broker_order_id, now=now
        )

    def submission_uncertain(
        self, client_order_id: str, *, reason: str,
        now: datetime | None = None,
    ) -> OmsOrder:
        if not reason.strip():
            raise ValueError("uncertain_submission_reason_required")
        return self.transition(
            client_order_id, OrderState.SUBMISSION_UNCERTAIN, reason=reason, now=now
        )

    def bind_broker_identity(
        self, client_order_id: str, *, broker_order_id: str, reason: str,
        now: datetime | None = None,
    ) -> OmsOrder:
        """Bind an externally observed broker ID without inventing a state transition."""
        self._validate_id(client_order_id, "client_order_id")
        self._validate_id(broker_order_id, "broker_order_id")
        if not reason.strip():
            raise ValueError("broker_identity_binding_reason_required")
        at = now or datetime.now(timezone.utc)
        with self.db:
            row = self.db.execute(
                "SELECT * FROM oms_orders WHERE client_order_id=?", (client_order_id,)
            ).fetchone()
            if row is None:
                raise KeyError(client_order_id)
            current = self._decode(row)
            broker = self._broker_identity(current.broker_order_id, broker_order_id)
            if current.broker_order_id == broker:
                return current
            self.db.execute(
                "UPDATE oms_orders SET broker_order_id=?,updated_at=? WHERE client_order_id=?",
                (broker, _instant(at, "broker_identity_observed_at"), client_order_id),
            )
            self._append_event_locked(client_order_id, "BROKER_ID_OBSERVED", at, {
                "brokerOrderId": broker, "reason": reason.strip(),
                "state": current.state.value,
            })
        return self.get(client_order_id)

    def reject(self, client_order_id: str, *, reason: str, now: datetime | None = None) -> OmsOrder:
        if not reason.strip():
            raise ValueError("order_rejection_reason_required")
        return self.transition(client_order_id, OrderState.REJECTED, reason=reason, now=now)

    def cancel(self, client_order_id: str, *, reason: str, now: datetime | None = None) -> OmsOrder:
        if not reason.strip():
            raise ValueError("order_cancel_reason_required")
        return self.transition(client_order_id, OrderState.CANCELLED, reason=reason, now=now)

    def replace_cancelled(
        self,
        original_client_order_id: str,
        replacement: OrderIntent,
        *,
        decision_id: str,
        reason: str,
        now: datetime | None = None,
    ) -> OmsOrder:
        """Create one conservative replacement after cancellation is confirmed.

        Replacement never mutates the old order and never races an unconfirmed cancel. The
        replacement may change price/protective levels and may reduce size, but it cannot
        increase the original order's still-unfilled quantity or change economic identity.
        """
        self._validate_id(original_client_order_id, "client_order_id")
        if not reason.strip():
            raise ValueError("replacement_reason_required")
        if (
            type(replacement.quantity) is not int
            or replacement.quantity <= 0
            or replacement.reference_price <= 0
        ):
            raise ValueError("oms_invalid_order_geometry")
        if not decision_id.strip():
            raise ValueError("decision_id_required")
        at = now or datetime.now(timezone.utc)
        stamp = _instant(at, "replacement_at")
        replacement_id = self.client_order_id(replacement, decision_id)
        with self.db:
            existing_link = self.db.execute(
                "SELECT * FROM oms_replacements WHERE original_client_order_id=?",
                (original_client_order_id,),
            ).fetchone()
            if existing_link is not None:
                if (
                    existing_link["replacement_client_order_id"] != replacement_id
                    or existing_link["reason"] != reason.strip()
                ):
                    raise ValueError("replacement_lineage_payload_mismatch")
                current = self.db.execute(
                    "SELECT * FROM oms_orders WHERE client_order_id=?", (replacement_id,)
                ).fetchone()
                if current is None:
                    raise ValueError("replacement_lineage_missing_order")
                decoded = self._decode(current)
                self._assert_same_intent(decoded, replacement)
                return decoded

            original_row = self.db.execute(
                "SELECT * FROM oms_orders WHERE client_order_id=?",
                (original_client_order_id,),
            ).fetchone()
            if original_row is None:
                raise KeyError(original_client_order_id)
            original = self._decode(original_row)
            if original.state is not OrderState.CANCELLED:
                raise ValueError("replacement_requires_confirmed_cancel")
            if original.pending_quantity <= 0:
                raise ValueError("cancelled_order_has_no_replaceable_quantity")
            if (
                original.tenant_id != replacement.tenant_id
                or original.strategy_id != replacement.strategy_id
                or original.symbol != replacement.symbol
                or original.market != replacement.market.value
                or original.asset_class != replacement.asset_class.value
                or original.side != replacement.side.value
            ):
                raise ValueError("replacement_economic_identity_changed")
            if replacement.quantity > original.pending_quantity:
                raise ValueError("replacement_exceeds_cancelled_remainder")
            collision = self.db.execute(
                "SELECT 1 FROM oms_orders WHERE client_order_id=?", (replacement_id,)
            ).fetchone()
            if collision is not None:
                raise ValueError("replacement_order_preexists_without_lineage")
            self.db.execute(
                "INSERT INTO oms_orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    replacement_id, replacement.tenant_id, replacement.strategy_id,
                    OrderState.CREATED.value, replacement.symbol, replacement.market.value,
                    replacement.asset_class.value, replacement.side.value, replacement.quantity,
                    str(replacement.reference_price), 0, None, None, stamp, stamp, 0,
                ),
            )
            self._append_event_locked(replacement_id, "CREATED", at, {
                "decisionId": decision_id,
                "replacesClientOrderId": original_client_order_id,
                "order": {
                    "tenantId": replacement.tenant_id,
                    "strategyId": replacement.strategy_id,
                    "market": replacement.market.value,
                    "assetClass": replacement.asset_class.value,
                    "symbol": replacement.symbol,
                    "side": replacement.side.value,
                    "quantity": replacement.quantity,
                    "referencePrice": str(replacement.reference_price),
                },
            })
            self.db.execute(
                "INSERT INTO oms_replacements VALUES(?,?,?,?)",
                (original_client_order_id, replacement_id, reason.strip(), stamp),
            )
            self._append_event_locked(original_client_order_id, "REPLACED_BY", at, {
                "replacementClientOrderId": replacement_id,
                "reason": reason.strip(),
            })
        return self.get(replacement_id)

    def replacement_for(self, original_client_order_id: str) -> str | None:
        self._validate_id(original_client_order_id, "client_order_id")
        row = self.db.execute(
            "SELECT replacement_client_order_id FROM oms_replacements "
            "WHERE original_client_order_id=?",
            (original_client_order_id,),
        ).fetchone()
        return None if row is None else str(row[0])

    def replacement_parent(self, replacement_client_order_id: str) -> str | None:
        self._validate_id(replacement_client_order_id, "client_order_id")
        row = self.db.execute(
            "SELECT original_client_order_id FROM oms_replacements "
            "WHERE replacement_client_order_id=?",
            (replacement_client_order_id,),
        ).fetchone()
        return None if row is None else str(row[0])

    def fill(
        self,
        client_order_id: str,
        *,
        fill_id: str,
        quantity: int,
        price: Decimal,
        broker_order_id: str | None = None,
        now: datetime | None = None,
    ) -> OmsOrder:
        self._validate_id(client_order_id, "client_order_id")
        self._validate_id(fill_id, "fill_id")
        if broker_order_id is not None:
            self._validate_id(broker_order_id, "broker_order_id")
        if type(quantity) is not int or quantity <= 0:
            raise ValueError("fill_quantity_must_be_positive_integer")
        fill_price = _decimal(price, "fill_price", positive=True)
        at = now or datetime.now(timezone.utc)
        with self.db:
            duplicate = self.db.execute("SELECT * FROM oms_fills WHERE fill_id=?", (fill_id,)).fetchone()
            if duplicate is not None:
                if (
                    duplicate["client_order_id"] != client_order_id
                    or duplicate["quantity"] != quantity
                    or _decimal(duplicate["price"], "fill_price") != fill_price
                    or duplicate["broker_order_id"] != broker_order_id
                ):
                    raise ValueError("fill_id_payload_mismatch")
                return self.get(client_order_id)
            row = self.db.execute(
                "SELECT * FROM oms_orders WHERE client_order_id=?", (client_order_id,)
            ).fetchone()
            if row is None:
                raise KeyError(client_order_id)
            current = self._decode(row)
            if current.state not in {
                OrderState.SUBMITTED, OrderState.SUBMISSION_UNCERTAIN, OrderState.PARTIALLY_FILLED
            }:
                raise ValueError(f"fill_not_allowed_from_state:{current.state.value}")
            total = current.filled_quantity + quantity
            if total > current.requested_quantity:
                raise ValueError("fill_exceeds_requested_quantity")
            prior_notional = (
                (current.average_fill_price or Decimal(0)) * current.filled_quantity
            )
            average = (prior_notional + fill_price * quantity) / total
            target = (
                OrderState.FILLED if total == current.requested_quantity
                else OrderState.PARTIALLY_FILLED
            )
            OrderLifecycle(current.state).transition(target)
            broker = self._broker_identity(current.broker_order_id, broker_order_id)
            stamp = _instant(at, "fill_at")
            self.db.execute(
                "INSERT INTO oms_fills VALUES(?,?,?,?,?,?)",
                (fill_id, client_order_id, quantity, str(fill_price), stamp, broker_order_id),
            )
            self.db.execute(
                """UPDATE oms_orders SET state=?,filled_quantity=?,average_fill_price=?,
                   broker_order_id=?,updated_at=? WHERE client_order_id=?""",
                (target.value, total, str(average), broker, stamp, client_order_id),
            )
            self._append_event_locked(client_order_id, "FILL", at, {
                "fillId": fill_id, "quantity": quantity, "price": str(fill_price),
                "cumulativeFilled": total, "averageFillPrice": str(average),
                "state": target.value, "brokerOrderId": broker,
            })
        return self.get(client_order_id)

    @staticmethod
    def _broker_identity(existing: str | None, supplied: str | None) -> str | None:
        if existing is not None and supplied is not None and existing != supplied:
            raise ValueError("broker_order_identity_changed")
        return existing or supplied

    @staticmethod
    def _validate_id(value: str, name: str) -> None:
        if not isinstance(value, str) or not _ID.fullmatch(value):
            raise ValueError(f"invalid_{name}")

    def _append_event_locked(
        self, client_order_id: str, kind: str, at: datetime, payload: dict[str, object]
    ) -> None:
        row = self.db.execute(
            "SELECT last_event_sequence FROM oms_orders WHERE client_order_id=?",
            (client_order_id,),
        ).fetchone()
        if row is None:
            raise KeyError(client_order_id)
        sequence = int(row[0]) + 1
        previous = self.db.execute(
            "SELECT event_hash FROM oms_events WHERE client_order_id=? ORDER BY sequence DESC LIMIT 1",
            (client_order_id,),
        ).fetchone()
        previous_hash = previous[0] if previous else None
        stamp = _instant(at, "oms_event_at")
        body = {
            "clientOrderId": client_order_id, "sequence": sequence, "kind": kind,
            "at": stamp, "previousHash": previous_hash, "payload": payload,
        }
        event_hash = _hash(body)
        self.db.execute(
            "INSERT INTO oms_events VALUES(?,?,?,?,?,?,?)",
            (client_order_id, sequence, kind, stamp, previous_hash, event_hash,
             _canonical(payload).decode()),
        )
        self.db.execute(
            "UPDATE oms_orders SET last_event_sequence=? WHERE client_order_id=?",
            (sequence, client_order_id),
        )

    def get(self, client_order_id: str) -> OmsOrder:
        self._validate_id(client_order_id, "client_order_id")
        row = self.db.execute(
            "SELECT * FROM oms_orders WHERE client_order_id=?", (client_order_id,)
        ).fetchone()
        if row is None:
            raise KeyError(client_order_id)
        return self._decode(row)

    def fill_ids(self, client_order_id: str) -> tuple[str, ...]:
        """Recorded fill identities, for external lifecycle reconciliation only."""
        self._validate_id(client_order_id, "client_order_id")
        if self.db.execute(
            "SELECT 1 FROM oms_orders WHERE client_order_id=?", (client_order_id,)
        ).fetchone() is None:
            raise KeyError(client_order_id)
        return tuple(
            str(row[0])
            for row in self.db.execute(
                "SELECT fill_id FROM oms_fills WHERE client_order_id=? ORDER BY at,fill_id",
                (client_order_id,),
            ).fetchall()
        )

    def open_orders(self, tenant_id: str) -> tuple[OmsOrder, ...]:
        rows = self.db.execute(
            "SELECT * FROM oms_orders WHERE tenant_id=? ORDER BY created_at,client_order_id",
            (tenant_id,),
        ).fetchall()
        return tuple(self._decode(row) for row in rows if OrderState(row["state"]) not in TERMINAL)

    def verify(self, client_order_id: str) -> dict[str, object]:
        """Replay the hash chain and compare event-derived fill totals to the projection."""
        current = self.get(client_order_id)
        events = self.db.execute(
            "SELECT * FROM oms_events WHERE client_order_id=? ORDER BY sequence",
            (client_order_id,),
        ).fetchall()
        previous = None
        fill_total = 0
        weighted = Decimal(0)
        for expected, row in enumerate(events, 1):
            if row["sequence"] != expected or row["previous_hash"] != previous:
                raise ValueError("oms_event_chain_incomplete")
            payload = json.loads(row["payload"])
            body = {
                "clientOrderId": client_order_id, "sequence": expected, "kind": row["kind"],
                "at": row["at"], "previousHash": previous, "payload": payload,
            }
            if row["event_hash"] != _hash(body):
                raise ValueError("oms_event_hash_mismatch")
            previous = row["event_hash"]
            if row["kind"] == "FILL":
                qty = int(payload["quantity"])
                price = _decimal(payload["price"], "fill_price", positive=True)
                fill_total += qty
                weighted += price * qty
        if len(events) != current.last_event_sequence:
            raise ValueError("oms_projection_event_sequence_mismatch")
        projected_average = weighted / fill_total if fill_total else None
        if fill_total != current.filled_quantity or projected_average != current.average_fill_price:
            raise ValueError("oms_fill_projection_mismatch")
        return {
            "schema": "pramana.oms_verification.v1",
            "clientOrderId": client_order_id,
            "state": current.state.value,
            "events": len(events),
            "filledQuantity": fill_total,
            "pendingQuantity": current.pending_quantity,
            "headHash": previous,
            "verified": True,
        }

    @staticmethod
    def _decode(row: sqlite3.Row) -> OmsOrder:
        average = row["average_fill_price"]
        return OmsOrder(
            row["client_order_id"], row["tenant_id"], row["strategy_id"],
            OrderState(row["state"]), row["symbol"], row["market"], row["asset_class"],
            row["side"], int(row["requested_quantity"]),
            _decimal(row["reference_price"], "reference_price", positive=True),
            int(row["filled_quantity"]),
            None if average is None else _decimal(average, "average_fill_price", positive=True),
            row["broker_order_id"], datetime.fromisoformat(row["created_at"]),
            datetime.fromisoformat(row["updated_at"]), int(row["last_event_sequence"]),
        )
