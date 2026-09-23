"""A paper capital contribution is a deposit: the books grow, the P&L and the drawdown do not.

Synthetic ledgers only. No provider, no network, no orders beyond the paper ledger.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant_ai.domain.models import AssetClass, Market, OrderIntent, Side
from quant_ai.execution.capital_contribution import (
    TABLE,
    ContributionError,
    contribute,
)
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.execution.portfolio import PortfolioTracker
from quant_ai.marketdata.feed import UsaSandboxMarketDataFeed

TENANT = "ghost"
NOW = datetime(2026, 9, 23, 10, 30, tzinfo=timezone.utc)


def order(side: Side, quantity: int, price: str) -> OrderIntent:
    return OrderIntent(
        "AAPL", Market.USA, side, quantity, Decimal(price), "test", AssetClass.EQUITY,
        TENANT, Decimal(90), Decimal(130),
    )


def ledger(tmp_path, *, price="100"):
    """A ₹1,00,000 account holding 100 shares bought at 100, now marked at ``price``."""
    path = tmp_path / "pramana.db"
    broker = PaperBrokerService(path, starting_capital=Decimal(100000), slippage_bps=Decimal(0))
    feed = UsaSandboxMarketDataFeed(base_price=Decimal(110))
    tracker = PortfolioTracker(broker, feed, tenant_id=TENANT)
    broker.buy(order(Side.BUY, 100, "100"))
    tracker.metrics(NOW)  # peak marked at 110
    feed.base_price = Decimal(price)
    return path, broker, feed, tracker


def operator(path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=5)
    connection.row_factory = sqlite3.Row
    return connection


def add(path, amount=900000, reference="topup-2026-09-23", reason="scale paper book", now=NOW):
    with operator(path) as connection:
        return contribute(connection, tenant_id=TENANT, amount=amount, reference=reference,
                          reason=reason, now=now)


def test_the_book_grows_by_the_deposit_and_still_reconciles(tmp_path):
    path, broker, _feed, _tracker = ledger(tmp_path)
    cash = broker.get_margin(TENANT).cash_balance

    record = add(path)

    assert record.recorded is True
    assert broker.get_starting_capital(TENANT) == Decimal(1_000_000)
    assert broker.get_margin(TENANT).cash_balance == cash + Decimal(900000)
    assert broker.reconcile(TENANT)["status"] == "matched"
    assert (record.capital_before, record.capital_after) == (Decimal(100000), Decimal(1_000_000))


def test_the_deposit_is_not_the_days_profit(tmp_path):
    path, _broker, _feed, tracker = ledger(tmp_path, price="105")
    before = tracker.metrics(NOW)

    add(path)
    after = tracker.metrics(NOW + timedelta(minutes=1))

    assert after.total_equity == before.total_equity + Decimal(900000)
    assert after.daily_total_pnl == before.daily_total_pnl


def test_a_deposit_before_the_first_mark_of_the_day_opens_the_day_with_it(tmp_path):
    path, _broker, _feed, tracker = ledger(tmp_path, price="105")
    yesterday_close = tracker.metrics(NOW).total_equity
    tomorrow = NOW + timedelta(days=1)

    record = add(path, now=tomorrow)
    after = tracker.metrics(tomorrow + timedelta(minutes=1))

    assert record.opening_after == yesterday_close + Decimal(900000)
    assert after.daily_total_pnl == Decimal(0)


def test_the_rupee_drawdown_already_suffered_is_kept(tmp_path):
    path, _broker, _feed, tracker = ledger(tmp_path, price="105")
    before = tracker.metrics(NOW)
    lost = before.high_water_mark - before.total_equity
    assert lost > 0

    add(path)
    after = tracker.metrics(NOW + timedelta(minutes=1))

    assert after.high_water_mark - after.total_equity == lost
    assert after.drawdown_fraction > 0


def test_a_running_tracker_picks_up_the_raised_peak(tmp_path):
    path, _broker, _feed, tracker = ledger(tmp_path, price="105")
    tracker.metrics(NOW)
    stale_peak = tracker._high_water_mark

    add(path)
    after = tracker.metrics(NOW + timedelta(minutes=1))

    assert after.high_water_mark == stale_peak + Decimal(900000)


def test_the_record_is_append_only_and_complete(tmp_path):
    path, *_ = ledger(tmp_path)
    record = add(path)

    with operator(path) as connection:
        row = connection.execute(f"SELECT * FROM {TABLE}").fetchone()
        assert row["amount"] == "900000" and row["reason"] == "scale paper book"
        assert row["risk_day"] == NOW.date().isoformat()
        assert Decimal(row["peak_after"]) == record.peak_before + Decimal(900000)
        for statement in (f"UPDATE {TABLE} SET amount='1'", f"DELETE FROM {TABLE}"):
            with pytest.raises(sqlite3.DatabaseError, match="append_only"):
                connection.execute(statement)


def test_the_same_reference_records_once(tmp_path):
    path, broker, *_ = ledger(tmp_path)
    add(path)

    again = add(path)

    assert again.recorded is False
    assert broker.get_starting_capital(TENANT) == Decimal(1_000_000)
    with operator(path) as connection:
        assert connection.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0] == 1


def test_a_reused_reference_with_another_amount_is_refused(tmp_path):
    path, broker, *_ = ledger(tmp_path)
    add(path)

    with pytest.raises(ContributionError, match="reference_reused"):
        add(path, amount=100000)
    assert broker.get_starting_capital(TENANT) == Decimal(1_000_000)


@pytest.mark.parametrize("amount", [0, -1, "1.5", "NaN", "Infinity", "10000001", "abc", None])
def test_an_invalid_amount_writes_nothing(tmp_path, amount):
    path, broker, *_ = ledger(tmp_path)
    before = broker.get_margin(TENANT).cash_balance

    with pytest.raises(ContributionError, match="amount_invalid"):
        add(path, amount=amount)

    assert broker.get_margin(TENANT).cash_balance == before
    with operator(path) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,)
        ).fetchone() is None


@pytest.mark.parametrize("field,value,code", [
    ("reference", "x", "reference_invalid"),
    ("reference", "bad reference", "reference_invalid"),
    ("reason", "  ", "reason_invalid"),
    ("reason", "a\nb", "reason_invalid"),
    ("now", NOW.replace(tzinfo=None), "clock_must_be_aware"),
])
def test_invalid_inputs_are_refused(tmp_path, field, value, code):
    path, *_ = ledger(tmp_path)
    with pytest.raises(ContributionError, match=code):
        add(path, **{field: value})


def test_an_unreconciled_ledger_is_not_topped_up(tmp_path):
    path, broker, *_ = ledger(tmp_path)
    broker._connection.execute(
        "UPDATE paper_accounts SET cash_balance='1' WHERE tenant_id=?", (TENANT,)
    )
    broker._connection.commit()

    with pytest.raises(ContributionError, match="unreconciled_before"):
        add(path)

    assert broker.get_starting_capital(TENANT) == Decimal(100000)


def test_a_missing_account_is_refused(tmp_path):
    path, *_ = ledger(tmp_path)
    with operator(path) as connection, pytest.raises(ContributionError, match="account_missing"):
        contribute(connection, tenant_id="nobody", amount=1000, reference="topup-1",
                   reason="r", now=NOW)


def test_a_connection_without_row_access_is_refused(tmp_path):
    path, *_ = ledger(tmp_path)
    with sqlite3.connect(path) as connection, pytest.raises(ContributionError, match="rows_unsupported"):
        contribute(connection, tenant_id=TENANT, amount=1000, reference="topup-1",
                   reason="r", now=NOW)
