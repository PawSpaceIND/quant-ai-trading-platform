from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant_ai.accounting.journal import EntrySide, Posting, TradingJournal, TransactionKind
from quant_ai.accounting.settlement import (
    SettlementBook,
    SettlementDirection,
    SettlementInstruction,
)
from quant_ai.accounting.trading import TradingAccounting

D = Decimal
NOW = datetime(2026, 9, 16, 10, tzinfo=timezone.utc)


def test_capital_margin_fee_and_realized_pnl_stay_double_entry_balanced(tmp_path):
    with TradingJournal(tmp_path / "journal.sqlite", base_currency="INR") as journal:
        accounting = TradingAccounting(journal, "tenant")
        accounting.seed_capital("capital", currency="INR", amount=D("100000"), base_amount=D("100000"), at=NOW)
        accounting.reserve_margin("margin", currency="INR", amount=D("10000"), base_amount=D("10000"), reference="GOLD margin", at=NOW)
        accounting.cash_fee("fee", currency="INR", amount=D("100"), base_amount=D("100"), reference="brokerage", at=NOW)
        accounting.realize_pnl("pnl", currency="INR", pnl=D("500"), base_pnl=D("500"), reference="GOLD close", at=NOW)
        assert journal.native_balance("tenant", "CASH_AVAILABLE", "INR") == D("90400")
        assert journal.native_balance("tenant", "CASH_RESERVED_MARGIN", "INR") == D("10000")
        assert journal.native_balance("tenant", "FEE_EXPENSE", "INR") == D("100")
        assert journal.native_balance("tenant", "REALIZED_PNL", "INR") == D("500")
        assert journal.verify("tenant")["verified"] is True


def test_non_fx_transaction_can_never_mix_currency_balances(tmp_path):
    with (
        TradingJournal(tmp_path / "journal.sqlite", base_currency="INR") as journal,
        pytest.raises(ValueError, match="native_currency_not_balanced"),
    ):
        journal.post(
            transaction_id="bad", tenant_id="tenant", kind=TransactionKind.ADJUSTMENT,
            reference="illegal currency net", at=NOW,
            postings=(
                Posting("CASH_AVAILABLE", EntrySide.DEBIT, "USD", D("100"), D("8300")),
                Posting("CAPITAL", EntrySide.CREDIT, "INR", D("8300"), D("8300")),
            ),
        )


def test_fx_conversion_requires_explicit_base_valuation_and_tracks_each_cashbook(tmp_path):
    with TradingJournal(tmp_path / "journal.sqlite", base_currency="INR") as journal:
        accounting = TradingAccounting(journal, "tenant")
        accounting.seed_capital("capital", currency="INR", amount=D("10000"), base_amount=D("10000"), at=NOW)
        accounting.convert_cash(
            "fx", from_currency="INR", from_amount=D("8300"),
            to_currency="USD", to_amount=D("100"), base_value=D("8300"),
            reference="explicit 83 INR/USD conversion", at=NOW,
        )
        assert journal.native_balance("tenant", "CASH_AVAILABLE", "INR") == D("1700")
        assert journal.native_balance("tenant", "CASH_AVAILABLE", "USD") == D("100")
        assert journal.base_balance("tenant", "CASH_AVAILABLE") == D("10000")
        with pytest.raises(ValueError, match="base_currency_not_balanced"):
            journal.post(
                transaction_id="badfx", tenant_id="tenant", kind=TransactionKind.FX_CONVERSION,
                reference="mismatched valuation", at=NOW,
                postings=(
                    Posting("CASH_AVAILABLE", EntrySide.DEBIT, "USD", D("1"), D("83")),
                    Posting("CASH_AVAILABLE", EntrySide.CREDIT, "INR", D("82"), D("82")),
                ),
            )


def test_transaction_id_is_idempotent_and_append_only_history_cannot_be_rewritten(tmp_path):
    with TradingJournal(tmp_path / "journal.sqlite", base_currency="INR") as journal:
        accounting = TradingAccounting(journal, "tenant")
        first = accounting.seed_capital("capital", currency="INR", amount=D("1000"), base_amount=D("1000"), at=NOW)
        same = accounting.seed_capital("capital", currency="INR", amount=D("1000"), base_amount=D("1000"), at=NOW)
        assert first.payload_sha256 == same.payload_sha256
        with pytest.raises(ValueError, match="transaction_id_payload_mismatch"):
            accounting.seed_capital("capital", currency="INR", amount=D("999"), base_amount=D("999"), at=NOW)
        with pytest.raises(Exception, match="append-only"):
            journal.db.execute("DELETE FROM trading_postings")


def test_settlement_date_is_explicit_and_receivable_only_becomes_cash_when_due(tmp_path):
    with TradingJournal(tmp_path / "journal.sqlite", base_currency="INR") as journal:
        accounting = TradingAccounting(journal, "tenant")
        accounting.seed_capital("capital", currency="INR", amount=D("1000"), base_amount=D("1000"), at=NOW)
        with SettlementBook(tmp_path / "settlement.sqlite", journal) as book:
            instruction = SettlementInstruction(
                "sell-1", "tenant", "equity sale proceeds", SettlementDirection.RECEIVABLE,
                "INR", D("500"), D("500"), NOW + timedelta(days=1),
            )
            book.open(instruction, at=NOW)
            assert journal.native_balance("tenant", "SETTLEMENT_RECEIVABLE", "INR") == D("500")
            assert journal.native_balance("tenant", "CASH_AVAILABLE", "INR") == D("1000")
            with pytest.raises(ValueError, match="settlement_not_due"):
                book.settle("sell-1", now=NOW + timedelta(hours=1))
            book.settle("sell-1", now=NOW + timedelta(days=1))
            assert journal.native_balance("tenant", "SETTLEMENT_RECEIVABLE", "INR") == D(0)
            assert journal.native_balance("tenant", "CASH_AVAILABLE", "INR") == D("1500")
            assert book.pending("tenant") == ()


def test_payable_settlement_reduces_cash_and_clears_liability(tmp_path):
    with TradingJournal(tmp_path / "journal.sqlite", base_currency="INR") as journal:
        accounting = TradingAccounting(journal, "tenant")
        accounting.seed_capital("capital", currency="INR", amount=D("1000"), base_amount=D("1000"), at=NOW)
        with SettlementBook(tmp_path / "settlement.sqlite", journal) as book:
            instruction = SettlementInstruction(
                "buy-1", "tenant", "equity purchase payable", SettlementDirection.PAYABLE,
                "INR", D("400"), D("400"), NOW,
            )
            book.open(instruction, at=NOW)
            assert journal.native_balance("tenant", "SETTLEMENT_PAYABLE", "INR") == D("400")
            book.settle("buy-1", now=NOW)
            assert journal.native_balance("tenant", "SETTLEMENT_PAYABLE", "INR") == D(0)
            assert journal.native_balance("tenant", "CASH_AVAILABLE", "INR") == D("600")
