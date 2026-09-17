"""Append-only double-entry trading subledger with explicit currency valuation.

Every transaction balances in the book's base currency. Non-FX transactions must also
balance in each native currency, preventing INR and USD from being netted by accident.
FX conversion is the only exception and requires explicit base-currency valuation on both
legs; no exchange rate is fetched or inferred here.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Self

_ID = re.compile(r"[A-Za-z0-9._:-]{1,160}")
_CURRENCY = re.compile(r"[A-Z]{3}")
_ACCOUNT = re.compile(r"[A-Z][A-Z0-9_]{1,63}")


class AccountType(str, Enum):
    ASSET = "ASSET"
    LIABILITY = "LIABILITY"
    EQUITY = "EQUITY"
    INCOME = "INCOME"
    EXPENSE = "EXPENSE"


class EntrySide(str, Enum):
    DEBIT = "DEBIT"
    CREDIT = "CREDIT"


class TransactionKind(str, Enum):
    CAPITAL = "CAPITAL"
    TRADE = "TRADE"
    FEE = "FEE"
    TAX = "TAX"
    MARGIN_RESERVE = "MARGIN_RESERVE"
    MARGIN_RELEASE = "MARGIN_RELEASE"
    REALIZED_PNL = "REALIZED_PNL"
    SETTLEMENT_OPEN = "SETTLEMENT_OPEN"
    SETTLEMENT_CLOSE = "SETTLEMENT_CLOSE"
    FX_CONVERSION = "FX_CONVERSION"
    ADJUSTMENT = "ADJUSTMENT"


@dataclass(frozen=True)
class Account:
    code: str
    account_type: AccountType

    def __post_init__(self) -> None:
        if not _ACCOUNT.fullmatch(self.code):
            raise ValueError("invalid_account_code")

    @property
    def normal_side(self) -> EntrySide:
        if self.account_type in {AccountType.ASSET, AccountType.EXPENSE}:
            return EntrySide.DEBIT
        return EntrySide.CREDIT


# A deliberately small canonical chart. Domain-specific services may add explicit accounts
# through ``register_account``; unknown codes never auto-create on a posting.
CANONICAL_ACCOUNTS = (
    Account("CASH_AVAILABLE", AccountType.ASSET),
    Account("CASH_RESERVED_MARGIN", AccountType.ASSET),
    Account("COLLATERAL_RESERVED", AccountType.ASSET),
    Account("SECURITIES_COST", AccountType.ASSET),
    Account("SETTLEMENT_RECEIVABLE", AccountType.ASSET),
    Account("SETTLEMENT_PAYABLE", AccountType.LIABILITY),
    Account("BROKER_CLEARING", AccountType.LIABILITY),
    Account("CAPITAL", AccountType.EQUITY),
    Account("REALIZED_PNL", AccountType.INCOME),
    Account("FEE_EXPENSE", AccountType.EXPENSE),
    Account("TAX_EXPENSE", AccountType.EXPENSE),
)


@dataclass(frozen=True)
class Posting:
    account: str
    side: EntrySide
    currency: str
    amount: Decimal
    base_amount: Decimal

    def __post_init__(self) -> None:
        if not _ACCOUNT.fullmatch(self.account):
            raise ValueError("invalid_posting_account")
        if not _CURRENCY.fullmatch(self.currency):
            raise ValueError("invalid_posting_currency")
        for value, label in ((self.amount, "amount"), (self.base_amount, "base_amount")):
            if not value.is_finite() or value <= 0:
                raise ValueError(f"posting_{label}_must_be_positive_finite")


@dataclass(frozen=True)
class JournalTransaction:
    transaction_id: str
    tenant_id: str
    kind: TransactionKind
    reference: str
    at: datetime
    postings: tuple[Posting, ...]
    payload_sha256: str


class TradingJournal:
    """Persistent append-only double-entry journal; no execution or pricing behavior."""

    def __init__(self, path: str | Path, *, base_currency: str) -> None:
        if not _CURRENCY.fullmatch(base_currency):
            raise ValueError("invalid_base_currency")
        self.base_currency = base_currency
        self.path = Path(path)
        if str(self.path) != ":memory:":
            if self.path.exists() and self.path.is_symlink():
                raise ValueError("trading_journal_symlink_unsupported")
            if not self.path.exists():
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
        self.db = sqlite3.connect(str(self.path), timeout=10, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=2000")
        self._schema()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def close(self) -> None:
        self.db.close()

    def _schema(self) -> None:
        with self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS trading_journal_meta(
                    id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL,
                    base_currency TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trading_accounts(
                    code TEXT PRIMARY KEY, account_type TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trading_transactions(
                    transaction_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                    kind TEXT NOT NULL, reference TEXT NOT NULL, at TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS trading_postings(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    transaction_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                    account TEXT NOT NULL, side TEXT NOT NULL, currency TEXT NOT NULL,
                    amount TEXT NOT NULL, base_amount TEXT NOT NULL,
                    UNIQUE(transaction_id, sequence)
                );
            """)
            meta = self.db.execute(
                "SELECT version,base_currency FROM trading_journal_meta WHERE id=1"
            ).fetchone()
            if meta is None:
                self.db.execute(
                    "INSERT INTO trading_journal_meta VALUES(1,1,?)", (self.base_currency,)
                )
            elif meta[0] != 1 or meta[1] != self.base_currency:
                raise ValueError("trading_journal_identity_mismatch")
            for account in CANONICAL_ACCOUNTS:
                existing = self.db.execute(
                    "SELECT account_type FROM trading_accounts WHERE code=?", (account.code,)
                ).fetchone()
                if existing is None:
                    self.db.execute(
                        "INSERT INTO trading_accounts VALUES(?,?)",
                        (account.code, account.account_type.value),
                    )
                elif existing[0] != account.account_type.value:
                    raise ValueError("trading_account_type_changed")
            for table in ("trading_transactions", "trading_postings"):
                for verb in ("UPDATE", "DELETE"):
                    self.db.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {table}_{verb.lower()}_blocked "
                        f"BEFORE {verb} ON {table} BEGIN "
                        "SELECT RAISE(ABORT,'Trading journal is append-only'); END"
                    )

    def register_account(self, account: Account) -> None:
        with self.db:
            row = self.db.execute(
                "SELECT account_type FROM trading_accounts WHERE code=?", (account.code,)
            ).fetchone()
            if row is None:
                self.db.execute(
                    "INSERT INTO trading_accounts VALUES(?,?)",
                    (account.code, account.account_type.value),
                )
            elif row[0] != account.account_type.value:
                raise ValueError("trading_account_type_changed")

    def post(
        self,
        *,
        transaction_id: str,
        tenant_id: str,
        kind: TransactionKind,
        reference: str,
        postings: tuple[Posting, ...],
        at: datetime | None = None,
    ) -> JournalTransaction:
        if not _ID.fullmatch(transaction_id) or not _ID.fullmatch(tenant_id):
            raise ValueError("invalid_transaction_or_tenant_id")
        if not reference.strip() or len(reference) > 500:
            raise ValueError("invalid_transaction_reference")
        moment = at or datetime.now(timezone.utc)
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("transaction_time_must_be_timezone_aware")
        if len(postings) < 2 or len(postings) > 100:
            raise ValueError("transaction_requires_bounded_postings")
        self._validate_accounts(postings)
        self._assert_balanced(kind, postings)
        stamp = moment.astimezone(timezone.utc).isoformat()
        payload = {
            "transactionId": transaction_id,
            "tenantId": tenant_id,
            "kind": kind.value,
            "reference": reference,
            "at": stamp,
            "postings": [
                {
                    "sequence": index,
                    "account": item.account,
                    "side": item.side.value,
                    "currency": item.currency,
                    "amount": str(item.amount),
                    "baseAmount": str(item.base_amount),
                }
                for index, item in enumerate(postings, 1)
            ],
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        with self.db:
            existing = self.db.execute(
                "SELECT payload_sha256 FROM trading_transactions WHERE transaction_id=?",
                (transaction_id,),
            ).fetchone()
            if existing is not None:
                if existing[0] != digest:
                    raise ValueError("transaction_id_payload_mismatch")
                return self.transaction(transaction_id)
            self.db.execute(
                "INSERT INTO trading_transactions VALUES(?,?,?,?,?,?)",
                (transaction_id, tenant_id, kind.value, reference, stamp, digest),
            )
            self.db.executemany(
                "INSERT INTO trading_postings(transaction_id,sequence,account,side,currency,amount,base_amount) VALUES(?,?,?,?,?,?,?)",
                [
                    (
                        transaction_id,
                        index,
                        item.account,
                        item.side.value,
                        item.currency,
                        str(item.amount),
                        str(item.base_amount),
                    )
                    for index, item in enumerate(postings, 1)
                ],
            )
        return JournalTransaction(
            transaction_id, tenant_id, kind, reference, moment, postings, digest
        )

    def _validate_accounts(self, postings: Iterable[Posting]) -> None:
        accounts = {
            row["code"]
            for row in self.db.execute("SELECT code FROM trading_accounts").fetchall()
        }
        missing = sorted({item.account for item in postings} - accounts)
        if missing:
            raise ValueError(f"unknown_trading_account:{','.join(missing)}")

    @staticmethod
    def _signed(item: Posting, *, base: bool) -> Decimal:
        amount = item.base_amount if base else item.amount
        return amount if item.side is EntrySide.DEBIT else -amount

    def _assert_balanced(
        self, kind: TransactionKind, postings: tuple[Posting, ...]
    ) -> None:
        base = sum((self._signed(item, base=True) for item in postings), Decimal(0))
        if base != 0:
            raise ValueError("transaction_base_currency_not_balanced")
        if kind is TransactionKind.FX_CONVERSION:
            currencies = {item.currency for item in postings}
            if len(currencies) != 2:
                raise ValueError("fx_conversion_requires_exactly_two_currencies")
            cash_accounts = {item.account for item in postings}
            if cash_accounts != {"CASH_AVAILABLE"}:
                raise ValueError("fx_conversion_must_move_available_cash_only")
            if len(postings) != 2:
                raise ValueError("fx_conversion_requires_two_cash_legs")
            return
        by_currency: dict[str, Decimal] = {}
        for item in postings:
            by_currency[item.currency] = by_currency.get(item.currency, Decimal(0)) + self._signed(
                item, base=False
            )
        if any(value != 0 for value in by_currency.values()):
            raise ValueError("transaction_native_currency_not_balanced")

    def transaction(self, transaction_id: str) -> JournalTransaction:
        row = self.db.execute(
            "SELECT * FROM trading_transactions WHERE transaction_id=?", (transaction_id,)
        ).fetchone()
        if row is None:
            raise KeyError(transaction_id)
        postings = tuple(
            Posting(
                item["account"], EntrySide(item["side"]), item["currency"],
                self._decimal(item["amount"]), self._decimal(item["base_amount"]),
            )
            for item in self.db.execute(
                "SELECT * FROM trading_postings WHERE transaction_id=? ORDER BY sequence",
                (transaction_id,),
            ).fetchall()
        )
        return JournalTransaction(
            row["transaction_id"], row["tenant_id"], TransactionKind(row["kind"]),
            row["reference"], datetime.fromisoformat(row["at"]), postings,
            row["payload_sha256"],
        )

    @staticmethod
    def _decimal(value: object) -> Decimal:
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError) as error:
            raise ValueError("invalid_journal_decimal") from error
        if not result.is_finite():
            raise ValueError("invalid_journal_decimal")
        return result

    def native_balance(self, tenant_id: str, account: str, currency: str) -> Decimal:
        if not _ACCOUNT.fullmatch(account) or not _CURRENCY.fullmatch(currency):
            raise ValueError("invalid_balance_query")
        account_row = self.db.execute(
            "SELECT account_type FROM trading_accounts WHERE code=?", (account,)
        ).fetchone()
        if account_row is None:
            raise KeyError(account)
        normal = Account(account, AccountType(account_row[0])).normal_side
        rows = self.db.execute(
            """SELECT p.side,p.amount FROM trading_postings p
               JOIN trading_transactions t ON t.transaction_id=p.transaction_id
               WHERE t.tenant_id=? AND p.account=? AND p.currency=?""",
            (tenant_id, account, currency),
        ).fetchall()
        return sum(
            (
                self._decimal(row["amount"])
                if EntrySide(row["side"]) is normal
                else -self._decimal(row["amount"])
                for row in rows
            ),
            Decimal(0),
        )

    def base_balance(self, tenant_id: str, account: str) -> Decimal:
        account_row = self.db.execute(
            "SELECT account_type FROM trading_accounts WHERE code=?", (account,)
        ).fetchone()
        if account_row is None:
            raise KeyError(account)
        normal = Account(account, AccountType(account_row[0])).normal_side
        rows = self.db.execute(
            """SELECT p.side,p.base_amount FROM trading_postings p
               JOIN trading_transactions t ON t.transaction_id=p.transaction_id
               WHERE t.tenant_id=? AND p.account=?""",
            (tenant_id, account),
        ).fetchall()
        return sum(
            (
                self._decimal(row["base_amount"])
                if EntrySide(row["side"]) is normal
                else -self._decimal(row["base_amount"])
                for row in rows
            ),
            Decimal(0),
        )

    def verify(self, tenant_id: str) -> dict[str, object]:
        rows = self.db.execute(
            "SELECT transaction_id FROM trading_transactions WHERE tenant_id=? ORDER BY at,transaction_id",
            (tenant_id,),
        ).fetchall()
        for row in rows:
            transaction = self.transaction(row[0])
            self._validate_accounts(transaction.postings)
            self._assert_balanced(transaction.kind, transaction.postings)
            payload = {
                "transactionId": transaction.transaction_id,
                "tenantId": transaction.tenant_id,
                "kind": transaction.kind.value,
                "reference": transaction.reference,
                "at": transaction.at.astimezone(timezone.utc).isoformat(),
                "postings": [
                    {
                        "sequence": index,
                        "account": item.account,
                        "side": item.side.value,
                        "currency": item.currency,
                        "amount": str(item.amount),
                        "baseAmount": str(item.base_amount),
                    }
                    for index, item in enumerate(transaction.postings, 1)
                ],
            }
            if transaction.payload_sha256 != hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest():
                raise ValueError("trading_transaction_hash_mismatch")
        return {
            "schema": "pramana.trading_journal_verification.v1",
            "tenantId": tenant_id,
            "baseCurrency": self.base_currency,
            "transactions": len(rows),
            "verified": True,
        }
