"""Explicit-date settlement and collateral tracking; no market calendar is guessed here."""
from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Self

from quant_ai.accounting.journal import EntrySide, Posting, TradingJournal, TransactionKind

_ID = re.compile(r"[A-Za-z0-9._:-]{1,160}")


class SettlementDirection(str, Enum):
    RECEIVABLE = "RECEIVABLE"
    PAYABLE = "PAYABLE"


class SettlementState(str, Enum):
    PENDING = "PENDING"
    SETTLED = "SETTLED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class SettlementInstruction:
    settlement_id: str
    tenant_id: str
    reference: str
    direction: SettlementDirection
    currency: str
    amount: Decimal
    base_amount: Decimal
    due_at: datetime

    def __post_init__(self) -> None:
        if not _ID.fullmatch(self.settlement_id) or not _ID.fullmatch(self.tenant_id):
            raise ValueError("invalid_settlement_identity")
        if not self.reference.strip():
            raise ValueError("settlement_reference_required")
        if self.due_at.tzinfo is None or self.due_at.utcoffset() is None:
            raise ValueError("settlement_due_at_must_be_timezone_aware")
        if not self.amount.is_finite() or self.amount <= 0:
            raise ValueError("settlement_amount_must_be_positive")
        if not self.base_amount.is_finite() or self.base_amount <= 0:
            raise ValueError("settlement_base_amount_must_be_positive")


class SettlementBook:
    """Persistent settlement obligations linked to balanced accounting postings."""

    def __init__(self, path: str | Path, journal: TradingJournal) -> None:
        self.path = Path(path)
        self.journal = journal
        if str(self.path) != ":memory:":
            if self.path.exists() and self.path.is_symlink():
                raise ValueError("settlement_book_symlink_unsupported")
            if not self.path.exists():
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
        self.db = sqlite3.connect(str(self.path), timeout=10)
        self.db.row_factory = sqlite3.Row
        with self.db:
            self.db.execute("""CREATE TABLE IF NOT EXISTS settlements(
                settlement_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, reference TEXT NOT NULL,
                direction TEXT NOT NULL, currency TEXT NOT NULL, amount TEXT NOT NULL,
                base_amount TEXT NOT NULL, due_at TEXT NOT NULL, state TEXT NOT NULL,
                opened_transaction_id TEXT NOT NULL, closed_transaction_id TEXT)""")
            for verb in ("DELETE",):
                self.db.execute(
                    f"CREATE TRIGGER IF NOT EXISTS settlements_{verb.lower()}_blocked BEFORE {verb} ON settlements BEGIN SELECT RAISE(ABORT,'Settlements are retained'); END"
                )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def close(self) -> None:
        self.db.close()

    def open(self, instruction: SettlementInstruction, *, at: datetime | None = None) -> None:
        existing = self.db.execute(
            "SELECT * FROM settlements WHERE settlement_id=?", (instruction.settlement_id,)
        ).fetchone()
        tx_id = f"settlement-open:{instruction.settlement_id}"
        if existing is not None:
            same = (
                existing["tenant_id"] == instruction.tenant_id
                and existing["reference"] == instruction.reference
                and existing["direction"] == instruction.direction.value
                and existing["currency"] == instruction.currency
                and Decimal(existing["amount"]) == instruction.amount
                and Decimal(existing["base_amount"]) == instruction.base_amount
                and datetime.fromisoformat(existing["due_at"]) == instruction.due_at
            )
            if not same:
                raise ValueError("settlement_id_payload_mismatch")
            return
        if instruction.direction is SettlementDirection.RECEIVABLE:
            postings = (
                Posting("SETTLEMENT_RECEIVABLE", EntrySide.DEBIT, instruction.currency, instruction.amount, instruction.base_amount),
                Posting("BROKER_CLEARING", EntrySide.CREDIT, instruction.currency, instruction.amount, instruction.base_amount),
            )
        else:
            postings = (
                Posting("BROKER_CLEARING", EntrySide.DEBIT, instruction.currency, instruction.amount, instruction.base_amount),
                Posting("SETTLEMENT_PAYABLE", EntrySide.CREDIT, instruction.currency, instruction.amount, instruction.base_amount),
            )
        self.journal.post(
            transaction_id=tx_id, tenant_id=instruction.tenant_id,
            kind=TransactionKind.SETTLEMENT_OPEN, reference=instruction.reference,
            postings=postings, at=at,
        )
        with self.db:
            self.db.execute(
                "INSERT INTO settlements VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    instruction.settlement_id, instruction.tenant_id, instruction.reference,
                    instruction.direction.value, instruction.currency, str(instruction.amount),
                    str(instruction.base_amount), instruction.due_at.astimezone(timezone.utc).isoformat(),
                    SettlementState.PENDING.value, tx_id, None,
                ),
            )

    def settle(
        self, settlement_id: str, *, now: datetime | None = None, allow_early: bool = False
    ) -> None:
        row = self.db.execute(
            "SELECT * FROM settlements WHERE settlement_id=?", (settlement_id,)
        ).fetchone()
        if row is None:
            raise KeyError(settlement_id)
        if row["state"] == SettlementState.SETTLED.value:
            return
        if row["state"] != SettlementState.PENDING.value:
            raise ValueError("settlement_not_pending")
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("settlement_now_must_be_timezone_aware")
        due = datetime.fromisoformat(row["due_at"])
        if moment < due and not allow_early:
            raise ValueError("settlement_not_due")
        amount, base = Decimal(row["amount"]), Decimal(row["base_amount"])
        if SettlementDirection(row["direction"]) is SettlementDirection.RECEIVABLE:
            postings = (
                Posting("CASH_AVAILABLE", EntrySide.DEBIT, row["currency"], amount, base),
                Posting("SETTLEMENT_RECEIVABLE", EntrySide.CREDIT, row["currency"], amount, base),
            )
        else:
            postings = (
                Posting("SETTLEMENT_PAYABLE", EntrySide.DEBIT, row["currency"], amount, base),
                Posting("CASH_AVAILABLE", EntrySide.CREDIT, row["currency"], amount, base),
            )
        tx_id = f"settlement-close:{settlement_id}"
        self.journal.post(
            transaction_id=tx_id, tenant_id=row["tenant_id"],
            kind=TransactionKind.SETTLEMENT_CLOSE, reference=row["reference"],
            postings=postings, at=moment,
        )
        with self.db:
            self.db.execute(
                "UPDATE settlements SET state=?,closed_transaction_id=? WHERE settlement_id=?",
                (SettlementState.SETTLED.value, tx_id, settlement_id),
            )

    def pending(self, tenant_id: str) -> tuple[SettlementInstruction, ...]:
        rows = self.db.execute(
            "SELECT * FROM settlements WHERE tenant_id=? AND state=? ORDER BY due_at,settlement_id",
            (tenant_id, SettlementState.PENDING.value),
        ).fetchall()
        return tuple(
            SettlementInstruction(
                row["settlement_id"], row["tenant_id"], row["reference"],
                SettlementDirection(row["direction"]), row["currency"], Decimal(row["amount"]),
                Decimal(row["base_amount"]), datetime.fromisoformat(row["due_at"]),
            )
            for row in rows
        )
