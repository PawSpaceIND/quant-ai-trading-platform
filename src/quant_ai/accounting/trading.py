"""Trading-domain postings over the generic double-entry journal."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from quant_ai.accounting.journal import EntrySide, Posting, TradingJournal, TransactionKind


class TradingAccounting:
    def __init__(self, journal: TradingJournal, tenant_id: str) -> None:
        self.journal = journal
        self.tenant_id = tenant_id

    def seed_capital(
        self, transaction_id: str, *, currency: str, amount: Decimal,
        base_amount: Decimal, at: datetime | None = None,
    ):
        return self.journal.post(
            transaction_id=transaction_id, tenant_id=self.tenant_id,
            kind=TransactionKind.CAPITAL, reference="initial capital", at=at,
            postings=(
                Posting("CASH_AVAILABLE", EntrySide.DEBIT, currency, amount, base_amount),
                Posting("CAPITAL", EntrySide.CREDIT, currency, amount, base_amount),
            ),
        )

    def reserve_margin(
        self, transaction_id: str, *, currency: str, amount: Decimal,
        base_amount: Decimal, reference: str, at: datetime | None = None,
    ):
        return self.journal.post(
            transaction_id=transaction_id, tenant_id=self.tenant_id,
            kind=TransactionKind.MARGIN_RESERVE, reference=reference, at=at,
            postings=(
                Posting("CASH_RESERVED_MARGIN", EntrySide.DEBIT, currency, amount, base_amount),
                Posting("CASH_AVAILABLE", EntrySide.CREDIT, currency, amount, base_amount),
            ),
        )

    def release_margin(
        self, transaction_id: str, *, currency: str, amount: Decimal,
        base_amount: Decimal, reference: str, at: datetime | None = None,
    ):
        return self.journal.post(
            transaction_id=transaction_id, tenant_id=self.tenant_id,
            kind=TransactionKind.MARGIN_RELEASE, reference=reference, at=at,
            postings=(
                Posting("CASH_AVAILABLE", EntrySide.DEBIT, currency, amount, base_amount),
                Posting("CASH_RESERVED_MARGIN", EntrySide.CREDIT, currency, amount, base_amount),
            ),
        )

    def cash_fee(
        self, transaction_id: str, *, currency: str, amount: Decimal,
        base_amount: Decimal, reference: str, tax: bool = False,
        at: datetime | None = None,
    ):
        expense = "TAX_EXPENSE" if tax else "FEE_EXPENSE"
        kind = TransactionKind.TAX if tax else TransactionKind.FEE
        return self.journal.post(
            transaction_id=transaction_id, tenant_id=self.tenant_id,
            kind=kind, reference=reference, at=at,
            postings=(
                Posting(expense, EntrySide.DEBIT, currency, amount, base_amount),
                Posting("CASH_AVAILABLE", EntrySide.CREDIT, currency, amount, base_amount),
            ),
        )

    def realize_pnl(
        self, transaction_id: str, *, currency: str, pnl: Decimal,
        base_pnl: Decimal, reference: str, at: datetime | None = None,
    ):
        if not pnl.is_finite() or not base_pnl.is_finite() or pnl == 0 or base_pnl == 0:
            raise ValueError("realized_pnl_must_be_nonzero_finite")
        if (pnl > 0) != (base_pnl > 0):
            raise ValueError("realized_pnl_native_and_base_sign_mismatch")
        amount, base_amount = abs(pnl), abs(base_pnl)
        if pnl > 0:
            postings = (
                Posting("CASH_AVAILABLE", EntrySide.DEBIT, currency, amount, base_amount),
                Posting("REALIZED_PNL", EntrySide.CREDIT, currency, amount, base_amount),
            )
        else:
            postings = (
                Posting("REALIZED_PNL", EntrySide.DEBIT, currency, amount, base_amount),
                Posting("CASH_AVAILABLE", EntrySide.CREDIT, currency, amount, base_amount),
            )
        return self.journal.post(
            transaction_id=transaction_id, tenant_id=self.tenant_id,
            kind=TransactionKind.REALIZED_PNL, reference=reference, postings=postings, at=at,
        )

    def convert_cash(
        self,
        transaction_id: str,
        *,
        from_currency: str,
        from_amount: Decimal,
        to_currency: str,
        to_amount: Decimal,
        base_value: Decimal,
        reference: str,
        at: datetime | None = None,
    ):
        if from_currency == to_currency:
            raise ValueError("fx_conversion_requires_different_currencies")
        return self.journal.post(
            transaction_id=transaction_id, tenant_id=self.tenant_id,
            kind=TransactionKind.FX_CONVERSION, reference=reference, at=at,
            postings=(
                Posting("CASH_AVAILABLE", EntrySide.DEBIT, to_currency, to_amount, base_value),
                Posting("CASH_AVAILABLE", EntrySide.CREDIT, from_currency, from_amount, base_value),
            ),
        )
