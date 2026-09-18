"""Post committed independent paper exits, never execute or invent missing entries.

This mirrors one declared native/base-currency paper account. All preceding strategy
fills must already have matching double-entry records; legacy or mixed-currency books
refuse. The separate accounting database is never in the protective execution path.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from quant_ai.accounting.journal import EntrySide, Posting, TransactionKind
from quant_ai.accounting.trading import TradingAccounting
from quant_ai.domain.models import AssetClass, Market, Side
from quant_ai.execution.derivative_margin import MARGINED_FUTURES_ASSET_CLASSES
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.instruments.identity import instrument_from_identity

TAX_CODES = frozenset({"STT", "CTT", "GST", "STAMP", "SEBI"})
D = Decimal


@dataclass(frozen=True)
class ProtectiveAccountingReport:
    status: str
    posted_order_ids: tuple[str, ...] = ()
    reason: str = "matched"
    source_sha256: str | None = None


@dataclass(frozen=True)
class _Batch:
    order_id: str
    transaction_id: str
    kind: TransactionKind
    reference: str
    at: datetime
    postings: tuple[Posting, ...]


class ProtectiveExitAccounting:
    """Replay-derived, idempotent accounting for completed protective exits only."""

    def __init__(self, broker: PaperBrokerService, accounting: TradingAccounting, *, currency: str):
        if currency not in {"INR", "USD"} or accounting.journal.base_currency != currency:
            raise ValueError("protective_accounting_native_base_currency_required")
        self.broker = broker
        self.accounting = accounting
        self.currency = currency

    def reconcile(self) -> ProtectiveAccountingReport:
        try:
            return self._reconcile()
        except (KeyError, TypeError, ValueError, ArithmeticError, sqlite3.Error, OSError, RuntimeError) as error:
            # No retry or broker action. Accounting failure must not stop protection.
            return ProtectiveAccountingReport("unavailable", (), f"protective_accounting:{error}")

    def _reconcile(self) -> ProtectiveAccountingReport:
        tenant = self.accounting.tenant_id
        with self.broker.accounting_read_snapshot():
            protected_ids = self.broker.protected_fill_ids(tenant)
            if not protected_ids:
                return ProtectiveAccountingReport("not_required", reason="no_protective_fills")
            receipts = {key: self.broker.protected_fill_receipt(key, tenant) for key in protected_ids}
            entries = self.broker.ledger_entries(tenant)
            costs = self.broker.cost_entries(tenant)
            account = self.broker._connection.execute(
                "SELECT starting_capital FROM paper_accounts WHERE tenant_id=?", (tenant,),
            ).fetchone()
            if account is None:
                raise ValueError("protective_accounting_account_missing")
            capital = D(account[0])
            if not capital.is_finite() or capital <= 0:
                raise ValueError("protective_accounting_capital_invalid")
        # Source reading has ended before the accounting write transaction starts.
        batches = self._batches(entries, costs)
        by_order = {entry.order_id: entry for entry in entries}
        if not set(receipts) <= set(by_order):
            raise ValueError("protective_accounting_orphan_receipt")
        for order_id, receipt in receipts.items():
            if receipt.entry != by_order[order_id]:
                raise ValueError("protective_accounting_receipt_entry_mismatch")
            total = sum((cost.amount for cost in costs if cost.order_id == order_id and cost.cash_debit), D(0))
            if D(receipt.evidence["fill"]["cash_fees"]) != total:
                raise ValueError("protective_accounting_fee_evidence_mismatch")
        source_sha = hashlib.sha256(json.dumps({
            "tenant": tenant, "currency": self.currency, "capital": str(capital),
            "entries": [vars(row) for row in entries], "costs": [vars(row) for row in costs],
            "receipts": {key: row.evidence for key, row in receipts.items()},
        }, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False).encode()).hexdigest()
        journal = self.accounting.journal
        posted = []
        with journal.atomic():
            journal.verify(tenant)
            expected_ids = {item.transaction_id for item in batches}
            rows = journal.db.execute(
                "SELECT transaction_id,kind FROM trading_transactions WHERE tenant_id=?", (tenant,),
            ).fetchall()
            for row in rows:
                if row["transaction_id"] in expected_ids:
                    continue
                transaction = journal.transaction(row["transaction_id"])
                if (transaction.kind is not TransactionKind.CAPITAL
                        or len(transaction.postings) != 2
                        or [(item.account, item.side) for item in transaction.postings] != [
                            ("CASH_AVAILABLE", EntrySide.DEBIT), ("CAPITAL", EntrySide.CREDIT)]
                        or any(item.currency != self.currency or item.base_amount != item.amount
                               for item in transaction.postings)):
                    raise ValueError("protective_accounting_unmapped_journal_transaction")
            if journal.native_balance(tenant, "CAPITAL", self.currency) != capital:
                raise ValueError("protective_accounting_capital_mismatch")
            missing = []
            for item in batches:
                try:
                    saved = journal.transaction(item.transaction_id)
                except KeyError:
                    if item.order_id not in receipts:
                        raise ValueError("protective_accounting_preceding_fill_unaccounted") from None
                    missing.append(item)
                    continue
                if (saved.tenant_id, saved.kind, saved.reference, saved.at, saved.postings) != (
                    tenant, item.kind, item.reference, item.at, item.postings
                ):
                    raise ValueError("protective_accounting_recorded_posting_mismatch")
            # All identity, basis, predecessor and existing-posting checks precede any write.
            for item in missing:
                journal.post(transaction_id=item.transaction_id, tenant_id=tenant,
                             kind=item.kind, reference=item.reference, at=item.at,
                             postings=item.postings)
                if item.order_id not in posted:
                    posted.append(item.order_id)
            journal.verify(tenant)
            expected_balances = {"CASH_AVAILABLE": capital, "CAPITAL": -capital}
            for item in batches:
                for posting in item.postings:
                    value = posting.amount if posting.side is EntrySide.DEBIT else -posting.amount
                    expected_balances[posting.account] = expected_balances.get(posting.account, D(0)) + value
            for code in ("CASH_AVAILABLE", "CASH_RESERVED_MARGIN", "SECURITIES_COST",
                         "REALIZED_PNL", "FEE_EXPENSE", "TAX_EXPENSE"):
                value = expected_balances.get(code, D(0))
                if code == "REALIZED_PNL":
                    value = -value
                if journal.native_balance(tenant, code, self.currency) != value:
                    raise ValueError("protective_accounting_balance_mismatch")
        # A later broker commit is not part of this snapshot; do not report it reconciled.
        if tuple(row.order_id for row in self.broker.ledger_entries(tenant)) != tuple(row.order_id for row in entries):
            return ProtectiveAccountingReport("pending", tuple(posted), "paper_ledger_advanced", source_sha)
        return ProtectiveAccountingReport("matched", tuple(posted), "matched", source_sha)

    def _batches(self, entries, costs) -> tuple[_Batch, ...]:
        result = []
        fees = {}
        known_ids = {entry.order_id for entry in entries}
        for cost in costs:
            if cost.order_id not in known_ids:
                raise ValueError("protective_accounting_orphan_fee")
            if cost.cash_debit and cost.amount > 0:
                key = (cost.order_id, cost.code)
                if key in fees:
                    raise ValueError("protective_accounting_duplicate_fee_code")
                fees[key] = cost
        positions = {}
        for entry in entries:
            if entry.asset_class not in ({AssetClass.EQUITY, AssetClass.ETF} | MARGINED_FUTURES_ASSET_CLASSES):
                raise ValueError("protective_accounting_asset_class_unsupported")
            if (entry.status != "FILLED" or entry.notional != entry.quantity * entry.fill_price
                    or entry.created_at.utcoffset() is None):
                raise ValueError("protective_accounting_fill_invalid")
            if entry.instrument_identity is not None:
                instrument = instrument_from_identity(entry.instrument_identity)
                currency = instrument.currency
            else:
                if entry.asset_class not in {AssetClass.EQUITY, AssetClass.ETF}:
                    raise ValueError("protective_accounting_unbound_derivative")
                currency = {Market.INDIA: "INR", Market.USA: "USD"}.get(entry.market)
            if currency != self.currency:
                raise ValueError("protective_accounting_currency_mismatch")
            key = (entry.symbol, entry.market, entry.asset_class)
            held, average, identity, reserved = positions.get(key, (0, D(0), None, D(0)))
            if held and identity != entry.instrument_identity:
                raise ValueError("protective_accounting_contract_history_mismatch")
            if entry.side is Side.SELL and entry.quantity > held:
                raise ValueError("protective_accounting_sale_without_position")
            reference = f"paper fill {entry.order_id} {entry.symbol}"
            def posting(code, side, value):
                return Posting(code, side, self.currency, value, value)
            def add(prefix, kind, lines, *, ref=reference, at=entry.created_at, order_id=entry.order_id):
                result.append(_Batch(order_id, f"{prefix}:{order_id}", kind, ref, at, tuple(lines)))
            debit, credit = EntrySide.DEBIT, EntrySide.CREDIT
            future = entry.asset_class in MARGINED_FUTURES_ASSET_CLASSES
            if future:
                change = entry.margin_change
                if change is None or not change.is_finite() or change == 0:
                    raise ValueError("protective_accounting_margin_missing")
                if entry.side is Side.BUY:
                    if change <= 0:
                        raise ValueError("protective_accounting_margin_sign_invalid")
                    add("margin", TransactionKind.MARGIN_RESERVE, [
                        posting("CASH_RESERVED_MARGIN", debit, change), posting("CASH_AVAILABLE", credit, change)])
                else:
                    if change != -(reserved * D(entry.quantity) / D(held)):
                        raise ValueError("protective_accounting_margin_release_mismatch")
                    add("margin", TransactionKind.MARGIN_RELEASE, [
                        posting("CASH_AVAILABLE", debit, -change), posting("CASH_RESERVED_MARGIN", credit, -change)])
                    pnl = (entry.fill_price - average) * entry.quantity
                    if pnl:
                        lines = ([posting("CASH_AVAILABLE", debit, pnl), posting("REALIZED_PNL", credit, pnl)]
                                 if pnl > 0 else [posting("REALIZED_PNL", debit, -pnl), posting("CASH_AVAILABLE", credit, -pnl)])
                        add("pnl", TransactionKind.REALIZED_PNL, lines)
                reserved += change
            elif entry.margin_change is not None:
                raise ValueError("protective_accounting_cash_margin_unexpected")
            elif entry.side is Side.BUY:
                add("trade", TransactionKind.TRADE, [posting("SECURITIES_COST", debit, entry.notional),
                                                      posting("CASH_AVAILABLE", credit, entry.notional)])
            else:
                basis = average * entry.quantity
                lines = [posting("CASH_AVAILABLE", debit, entry.notional), posting("SECURITIES_COST", credit, basis)]
                pnl = entry.notional - basis
                if pnl:
                    lines.append(posting("REALIZED_PNL", credit if pnl > 0 else debit, abs(pnl)))
                add("trade", TransactionKind.TRADE, lines)
            if entry.side is Side.BUY:
                average = (average * held + entry.notional) / (held + entry.quantity)
                held += entry.quantity
            else:
                held -= entry.quantity
            positions[key] = (held, average if held else D(0), entry.instrument_identity if held else None, reserved)
            for (order_id, code), cost in fees.items():
                if order_id == entry.order_id:
                    tax = code in TAX_CODES
                    result.append(_Batch(order_id, f"cost:{order_id}:{code}", TransactionKind.TAX if tax else TransactionKind.FEE,
                        f"{reference} cost {code}", cost.created_at,
                        (posting("TAX_EXPENSE" if tax else "FEE_EXPENSE", debit, cost.amount),
                         posting("CASH_AVAILABLE", credit, cost.amount))))
        return tuple(result)
