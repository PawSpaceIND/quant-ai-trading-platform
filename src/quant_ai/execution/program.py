"""Durable execution-program journal for planned child orders.

The program is a schedule, not an order transport. A child becomes executable only when its
scheduled timestamp is due; committed slices are never replayed after restart.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Self

from quant_ai.execution.planner import ExecutionPlan

_ID = re.compile(r"[A-Za-z0-9._:-]{1,180}")


class ProgramState(str, Enum):
    PLANNED = "PLANNED"
    ACTIVE = "ACTIVE"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class SliceState(str, Enum):
    PENDING = "PENDING"
    FILLED_UNACCOUNTED = "FILLED_UNACCOUNTED"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class ProgramSlice:
    sequence: int
    scheduled_at: datetime
    quantity: int
    state: SliceState
    client_order_id: str | None = None
    broker_order_id: str | None = None
    pre_fill_average_price: str | None = None
    failure_reason: str | None = None


@dataclass(frozen=True)
class ExecutionProgram:
    program_id: str
    tenant_id: str
    decision_id: str
    symbol: str
    state: ProgramState
    plan_sha256: str
    runtime_context_sha256: str
    parent_quantity: int
    created_at: datetime
    slices: tuple[ProgramSlice, ...]
    parent_order_payload: str | None = None

    @property
    def executed_quantity(self) -> int:
        return sum(item.quantity for item in self.slices if item.state is SliceState.EXECUTED)

    @property
    def pending_quantity(self) -> int:
        return sum(item.quantity for item in self.slices if item.state is SliceState.PENDING)


class ExecutionProgramJournal:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            if self.path.exists() and self.path.is_symlink():
                raise ValueError("execution_program_symlink_unsupported")
            if not self.path.exists():
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
        self.db = sqlite3.connect(str(self.path), timeout=10, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        with self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS execution_programs(
                    program_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                    decision_id TEXT NOT NULL, symbol TEXT NOT NULL,
                    state TEXT NOT NULL, plan_sha256 TEXT NOT NULL,
                    runtime_context_sha256 TEXT NOT NULL,
                    parent_quantity INTEGER NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(tenant_id,decision_id)
                );
                CREATE TABLE IF NOT EXISTS execution_program_slices(
                    program_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                    scheduled_at TEXT NOT NULL, quantity INTEGER NOT NULL,
                    state TEXT NOT NULL, client_order_id TEXT,
                    broker_order_id TEXT, pre_fill_average_price TEXT, failure_reason TEXT,
                    PRIMARY KEY(program_id,sequence)
                );
            """)
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(execution_programs)")}
            if "parent_order_payload" not in columns:
                self.db.execute("ALTER TABLE execution_programs ADD COLUMN parent_order_payload TEXT")
            self.db.execute(
                """CREATE TRIGGER IF NOT EXISTS execution_program_slice_delete_blocked
                   BEFORE DELETE ON execution_program_slices BEGIN
                   SELECT RAISE(ABORT,'Execution program history is retained'); END"""
            )
            self.db.execute(
                """CREATE TRIGGER IF NOT EXISTS execution_program_delete_blocked
                   BEFORE DELETE ON execution_programs BEGIN
                   SELECT RAISE(ABORT,'Execution program history is retained'); END"""
            )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def close(self) -> None:
        self.db.close()

    @staticmethod
    def _plan_payload(plan: ExecutionPlan) -> dict[str, object]:
        return {
            "algorithm": plan.algorithm.value,
            "parentQuantity": plan.parent_quantity,
            "lotSize": plan.lot_size,
            "source": plan.source,
            "slices": [
                {
                    "sequence": item.sequence,
                    "at": item.at.astimezone(timezone.utc).isoformat(),
                    "quantity": item.quantity,
                    "observedAvailableQuantity": item.observed_available_quantity,
                    "participation": None if item.participation is None else str(item.participation),
                }
                for item in plan.slices
            ],
        }

    def create(
        self,
        *,
        program_id: str,
        tenant_id: str,
        decision_id: str,
        symbol: str,
        plan: ExecutionPlan,
        runtime_context_sha256: str,
        created_at: datetime,
        parent_order_payload: str | None = None,
    ) -> ExecutionProgram:
        for value in (program_id, tenant_id, decision_id, symbol):
            if not _ID.fullmatch(value):
                raise ValueError("execution_program_identity_invalid")
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("execution_program_created_at_must_be_timezone_aware")
        if (
            len(runtime_context_sha256) != 64
            or any(ch not in "0123456789abcdef" for ch in runtime_context_sha256)
        ):
            raise ValueError("execution_program_runtime_context_digest_invalid")
        plan.assert_conservative()
        payload = self._plan_payload(plan)
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        existing = self.db.execute(
            "SELECT * FROM execution_programs WHERE tenant_id=? AND decision_id=?",
            (tenant_id, decision_id),
        ).fetchone()
        if existing is not None:
            if (
                existing["program_id"] != program_id
                or existing["symbol"] != symbol
                or existing["plan_sha256"] != digest
                or existing["runtime_context_sha256"] != runtime_context_sha256
                or existing["parent_quantity"] != plan.parent_quantity
                or existing["parent_order_payload"] != parent_order_payload
            ):
                raise ValueError("execution_program_decision_payload_mismatch")
            return self.get(program_id)
        with self.db:
            self.db.execute(
                """INSERT INTO execution_programs
                (program_id,tenant_id,decision_id,symbol,state,plan_sha256,runtime_context_sha256,
                 parent_quantity,created_at,parent_order_payload) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    program_id, tenant_id, decision_id, symbol, ProgramState.PLANNED.value,
                    digest, runtime_context_sha256, plan.parent_quantity,
                    created_at.astimezone(timezone.utc).isoformat(), parent_order_payload,
                ),
            )
            self.db.executemany(
                "INSERT INTO execution_program_slices VALUES(?,?,?,?,?,?,?,?,?)",
                [
                    (
                        program_id, item.sequence, item.at.astimezone(timezone.utc).isoformat(),
                        item.quantity, SliceState.PENDING.value, None, None, None, None,
                    )
                    for item in plan.slices
                ],
            )
        return self.get(program_id)

    def due(self, program_id: str, now: datetime) -> tuple[ProgramSlice, ...]:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("execution_program_now_must_be_timezone_aware")
        program = self.get(program_id)
        if program.state in {ProgramState.COMPLETE, ProgramState.CANCELLED, ProgramState.FAILED}:
            return ()
        instant = now.astimezone(timezone.utc)
        return tuple(
            item for item in program.slices
            if item.state is SliceState.PENDING
            and item.scheduled_at.astimezone(timezone.utc) <= instant
        )

    def mark_filled_unaccounted(
        self,
        program_id: str,
        sequence: int,
        *,
        client_order_id: str,
        broker_order_id: str,
        pre_fill_average_price: str | None,
    ) -> ExecutionProgram:
        current = self._slice(program_id, sequence)
        if current.state is SliceState.FILLED_UNACCOUNTED:
            if (
                current.client_order_id != client_order_id
                or current.broker_order_id != broker_order_id
                or current.pre_fill_average_price != pre_fill_average_price
            ):
                raise ValueError("filled_unaccounted_slice_identity_changed")
            return self.get(program_id)
        if current.state is SliceState.EXECUTED:
            return self.get(program_id)
        if current.state is not SliceState.PENDING:
            raise ValueError("execution_slice_not_pending")
        with self.db:
            self.db.execute(
                """UPDATE execution_program_slices SET state=?,client_order_id=?,broker_order_id=?,
                   pre_fill_average_price=? WHERE program_id=? AND sequence=?""",
                (
                    SliceState.FILLED_UNACCOUNTED.value, client_order_id, broker_order_id,
                    pre_fill_average_price, program_id, sequence,
                ),
            )
            self.db.execute(
                "UPDATE execution_programs SET state=? WHERE program_id=?",
                (ProgramState.ACTIVE.value, program_id),
            )
        return self.get(program_id)

    def mark_executed(
        self,
        program_id: str,
        sequence: int,
        *,
        client_order_id: str,
        broker_order_id: str,
    ) -> ExecutionProgram:
        current = self._slice(program_id, sequence)
        if current.state is SliceState.EXECUTED:
            if (
                current.client_order_id != client_order_id
                or current.broker_order_id != broker_order_id
            ):
                raise ValueError("executed_slice_identity_changed")
            return self.get(program_id)
        if current.state is not SliceState.FILLED_UNACCOUNTED:
            raise ValueError("execution_slice_not_filled_unaccounted")
        if (
            current.client_order_id != client_order_id
            or current.broker_order_id != broker_order_id
        ):
            raise ValueError("filled_unaccounted_slice_identity_changed")
        with self.db:
            self.db.execute(
                """UPDATE execution_program_slices SET state=?,client_order_id=?,broker_order_id=?
                   WHERE program_id=? AND sequence=?""",
                (
                    SliceState.EXECUTED.value, client_order_id, broker_order_id,
                    program_id, sequence,
                ),
            )
            self._refresh_state(program_id)
        return self.get(program_id)

    def unaccounted(self, program_id: str) -> tuple[ProgramSlice, ...]:
        return tuple(
            item for item in self.get(program_id).slices
            if item.state is SliceState.FILLED_UNACCOUNTED
        )

    def mark_failed(self, program_id: str, sequence: int, reason: str) -> ExecutionProgram:
        if not reason.strip():
            raise ValueError("execution_slice_failure_reason_required")
        current = self._slice(program_id, sequence)
        if current.state is not SliceState.PENDING:
            raise ValueError("execution_slice_not_pending")
        with self.db:
            self.db.execute(
                """UPDATE execution_program_slices SET state=?,failure_reason=?
                   WHERE program_id=? AND sequence=?""",
                (SliceState.FAILED.value, reason, program_id, sequence),
            )
            self.db.execute(
                "UPDATE execution_programs SET state=? WHERE program_id=?",
                (ProgramState.FAILED.value, program_id),
            )
        return self.get(program_id)

    def _refresh_state(self, program_id: str) -> None:
        states = [
            SliceState(row[0])
            for row in self.db.execute(
                "SELECT state FROM execution_program_slices WHERE program_id=? ORDER BY sequence",
                (program_id,),
            ).fetchall()
        ]
        state = (
            ProgramState.COMPLETE
            if states and all(item is SliceState.EXECUTED for item in states)
            else ProgramState.ACTIVE
        )
        self.db.execute(
            "UPDATE execution_programs SET state=? WHERE program_id=?", (state.value, program_id)
        )

    def _slice(self, program_id: str, sequence: int) -> ProgramSlice:
        row = self.db.execute(
            "SELECT * FROM execution_program_slices WHERE program_id=? AND sequence=?",
            (program_id, sequence),
        ).fetchone()
        if row is None:
            raise KeyError((program_id, sequence))
        return self._decode_slice(row)

    @staticmethod
    def _decode_slice(row: sqlite3.Row) -> ProgramSlice:
        return ProgramSlice(
            int(row["sequence"]), datetime.fromisoformat(row["scheduled_at"]),
            int(row["quantity"]), SliceState(row["state"]), row["client_order_id"],
            row["broker_order_id"], row["pre_fill_average_price"], row["failure_reason"],
        )

    def get(self, program_id: str) -> ExecutionProgram:
        row = self.db.execute(
            "SELECT * FROM execution_programs WHERE program_id=?", (program_id,)
        ).fetchone()
        if row is None:
            raise KeyError(program_id)
        slices = tuple(
            self._decode_slice(item)
            for item in self.db.execute(
                "SELECT * FROM execution_program_slices WHERE program_id=? ORDER BY sequence",
                (program_id,),
            ).fetchall()
        )
        if sum(item.quantity for item in slices) != int(row["parent_quantity"]):
            raise ValueError("execution_program_quantity_projection_mismatch")
        return ExecutionProgram(
            row["program_id"], row["tenant_id"], row["decision_id"], row["symbol"],
            ProgramState(row["state"]), row["plan_sha256"], row["runtime_context_sha256"],
            int(row["parent_quantity"]), datetime.fromisoformat(row["created_at"]), slices,
            row["parent_order_payload"],
        )
