"""Add capital to a paper account as a recorded deposit, never as profit.

A paper account is funded once, when the ledger first sees the tenant. Raising that
figure in the founder directives later changes the risk plan but not the book: the
account row already exists, the sizer works from the ledger's equity, and a hand edit
of the cash would fail reconciliation. This is the one supported way to add capital.

One SQLite transaction moves every figure that assumes capital never changes:

* ``paper_accounts.starting_capital`` and ``cash_balance`` rise together, so every
  replay (``reconcile_paper``, the institutional cash book) still finds
  cash = capital + fills - costs.
* ``peak_equity`` rises by the same amount, so the rupee drawdown already suffered is
  kept rather than erased by the deposit.
* today's (UTC) ``risk_daily_equity`` opening rises too, so the deposit is not booked as
  the day's profit, which would otherwise switch the daily-loss breaker off for the day.
* an append-only row in ``paper_capital_contributions`` records the amount, the reason
  and every before/after figure.

Reconciliation runs inside the same transaction, before and after the change; if the
account does not reconcile either way nothing is written. A reference names each
contribution, so running the same command twice records it once.

The double-entry trading journal (``quant_ai.accounting``) is a separate file that the
paper pilot does not use. An operator who runs that mode must post the matching CAPITAL
transaction to it; this module does not reach into a second database it cannot commit
atomically with the first.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from quant_ai.execution.reconciliation import reconcile_paper

TABLE = "paper_capital_contributions"
# A fat-finger ceiling for one contribution, in account currency.
MAX_CONTRIBUTION = Decimal(10_000_000)
REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{2,63}")
MAX_REASON = 200

# Separate statements: ``executescript`` would commit the open transaction first. The
# table is created by the first contribution only, so replay ledgers never carry it.
SCHEMA = (
    f"""CREATE TABLE IF NOT EXISTS {TABLE} (
        tenant_id TEXT NOT NULL,
        reference TEXT NOT NULL,
        amount TEXT NOT NULL,
        reason TEXT NOT NULL,
        contributed_at TEXT NOT NULL,
        capital_before TEXT NOT NULL,
        capital_after TEXT NOT NULL,
        cash_before TEXT NOT NULL,
        cash_after TEXT NOT NULL,
        peak_before TEXT,
        peak_after TEXT,
        risk_day TEXT,
        opening_before TEXT,
        opening_after TEXT,
        PRIMARY KEY (tenant_id, reference)
    )""",
    f"""CREATE TRIGGER IF NOT EXISTS {TABLE}_no_update BEFORE UPDATE ON {TABLE}
    BEGIN SELECT RAISE(ABORT, 'capital_contribution_append_only'); END""",
    f"""CREATE TRIGGER IF NOT EXISTS {TABLE}_no_delete BEFORE DELETE ON {TABLE}
    BEGIN SELECT RAISE(ABORT, 'capital_contribution_append_only'); END""",
)


class ContributionError(ValueError):
    """A fixed refusal code; nothing was written."""


@dataclass(frozen=True)
class Contribution:
    tenant_id: str
    reference: str
    amount: Decimal
    reason: str
    contributed_at: str
    capital_before: Decimal
    capital_after: Decimal
    cash_before: Decimal
    cash_after: Decimal
    peak_before: Decimal | None
    peak_after: Decimal | None
    risk_day: str | None
    opening_before: Decimal | None
    opening_after: Decimal | None
    recorded: bool


def _amount(value: object) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ContributionError("capital_contribution_amount_invalid") from None
    if (not amount.is_finite() or amount <= 0 or amount > MAX_CONTRIBUTION
            or amount != amount.to_integral_value()):
        raise ContributionError("capital_contribution_amount_invalid")
    return amount


def _money(value: object, code: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ContributionError(code) from None
    if not result.is_finite():
        raise ContributionError(code)
    return result


def _optional(value: object, code: str) -> Decimal | None:
    return None if value is None else _money(value, code)


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _from_row(row: sqlite3.Row) -> Contribution:
    return Contribution(
        row["tenant_id"], row["reference"], Decimal(row["amount"]), row["reason"],
        row["contributed_at"], Decimal(row["capital_before"]), Decimal(row["capital_after"]),
        Decimal(row["cash_before"]), Decimal(row["cash_after"]),
        _optional(row["peak_before"], "capital_contribution_record_invalid"),
        _optional(row["peak_after"], "capital_contribution_record_invalid"),
        row["risk_day"],
        _optional(row["opening_before"], "capital_contribution_record_invalid"),
        _optional(row["opening_after"], "capital_contribution_record_invalid"),
        recorded=False,
    )


def _has_table(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _reconciled(connection: sqlite3.Connection, tenant_id: str, code: str) -> None:
    report = reconcile_paper(connection, tenant_id)
    if report.get("status") != "matched":
        raise ContributionError(code)


def contribute(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    amount: object,
    reference: str,
    reason: str,
    now: datetime,
) -> Contribution:
    """Record one contribution, or return the one already recorded under ``reference``.

    ``connection`` must be a read-write connection to the live paper ledger with
    ``row_factory = sqlite3.Row``. Refusals raise ``ContributionError`` and write nothing.
    """
    value = _amount(amount)
    if not isinstance(reference, str) or not REFERENCE.fullmatch(reference):
        raise ContributionError("capital_contribution_reference_invalid")
    if (not isinstance(reason, str) or not reason.strip() or len(reason) > MAX_REASON
            or not reason.isprintable()):
        raise ContributionError("capital_contribution_reason_invalid")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ContributionError("capital_contribution_clock_must_be_aware")
    moment = now.astimezone(timezone.utc)
    if connection.row_factory is not sqlite3.Row:
        raise ContributionError("capital_contribution_connection_rows_unsupported")
    if connection.in_transaction:
        raise ContributionError("capital_contribution_connection_busy")
    connection.execute("BEGIN IMMEDIATE")
    try:
        account = connection.execute(
            "SELECT starting_capital, cash_balance, peak_equity FROM paper_accounts "
            "WHERE tenant_id=?", (tenant_id,),
        ).fetchone()
        if account is None:
            raise ContributionError("capital_contribution_account_missing")
        for statement in SCHEMA:
            connection.execute(statement)
        earlier = connection.execute(
            f"SELECT * FROM {TABLE} WHERE tenant_id=? AND reference=?", (tenant_id, reference),
        ).fetchone()
        if earlier is not None:
            if Decimal(earlier["amount"]) != value:
                raise ContributionError("capital_contribution_reference_reused")
            connection.rollback()
            return _from_row(earlier)
        _reconciled(connection, tenant_id, "capital_contribution_unreconciled_before")
        capital = _money(account["starting_capital"], "capital_contribution_account_invalid")
        cash = _money(account["cash_balance"], "capital_contribution_account_invalid")
        peak = _optional(account["peak_equity"], "capital_contribution_account_invalid")
        if capital <= 0:
            raise ContributionError("capital_contribution_account_invalid")
        # No stored peak means equity has never marked above the starting capital, and the
        # tracker then measures drawdown from the starting capital itself. Carry that
        # figure forward: left empty, a running tracker would record the post-deposit
        # equity as a fresh peak and erase the drawdown already taken (23 September 2026).
        new_peak = (capital if peak is None else peak) + value
        connection.execute(
            "UPDATE paper_accounts SET starting_capital=?, cash_balance=?, peak_equity=?, "
            "updated_at=? WHERE tenant_id=?",
            (str(capital + value), str(cash + value), _text(new_peak),
             moment.isoformat(), tenant_id),
        )
        risk_day = opening_before = opening_after = None
        if _has_table(connection, "risk_daily_equity"):
            day = moment.date().isoformat()
            today = connection.execute(
                "SELECT opening_equity, last_equity FROM risk_daily_equity "
                "WHERE tenant_id=? AND valuation_date=?", (tenant_id, day),
            ).fetchone()
            if today is not None:
                opening_before = _money(today["opening_equity"], "capital_contribution_risk_day_invalid")
                last = _money(today["last_equity"], "capital_contribution_risk_day_invalid")
                opening_after = opening_before + value
                connection.execute(
                    "UPDATE risk_daily_equity SET opening_equity=?, last_equity=?, updated_at=? "
                    "WHERE tenant_id=? AND valuation_date=?",
                    (str(opening_after), str(last + value), moment.isoformat(), tenant_id, day),
                )
                risk_day = day
            else:
                prior = connection.execute(
                    "SELECT last_equity FROM risk_daily_equity WHERE tenant_id=? "
                    "AND valuation_date<? ORDER BY valuation_date DESC LIMIT 1", (tenant_id, day),
                ).fetchone()
                if prior is not None:
                    # Today would open at yesterday's close; open it here, deposit included.
                    last = _money(prior["last_equity"], "capital_contribution_risk_day_invalid")
                    opening_after = last + value
                    connection.execute(
                        "INSERT INTO risk_daily_equity (tenant_id, valuation_date, "
                        "opening_equity, last_equity, updated_at) VALUES (?, ?, ?, ?, ?)",
                        (tenant_id, day, str(opening_after), str(opening_after), moment.isoformat()),
                    )
                    risk_day = day
        _reconciled(connection, tenant_id, "capital_contribution_unreconciled_after")
        record = Contribution(
            tenant_id, reference, value, reason.strip(), moment.isoformat(),
            capital, capital + value, cash, cash + value, peak, new_peak,
            risk_day, opening_before, opening_after, recorded=True,
        )
        connection.execute(
            f"INSERT INTO {TABLE} (tenant_id, reference, amount, reason, contributed_at, "
            "capital_before, capital_after, cash_before, cash_after, peak_before, peak_after, "
            "risk_day, opening_before, opening_after) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (record.tenant_id, record.reference, str(record.amount), record.reason,
             record.contributed_at, str(record.capital_before), str(record.capital_after),
             str(record.cash_before), str(record.cash_after), _text(record.peak_before),
             _text(record.peak_after), record.risk_day, _text(record.opening_before),
             _text(record.opening_after)),
        )
        connection.commit()
        return record
    except BaseException:
        connection.rollback()
        raise
