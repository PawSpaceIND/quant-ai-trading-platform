"""Finishing the day flat: covering every open position, and proving the book is empty."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from quant_ai.domain.models import Market, PortfolioSnapshot, Side
from quant_ai.risk.session_flatten import (
    FLATTEN_STRATEGY_ID,
    FlattenIntent,
    FlattenPolicy,
    plan_session_flatten,
    verify_flat,
)

IST = ZoneInfo("Asia/Kolkata")


class Session:
    timezone = "Asia/Kolkata"

    def closes_on(self, day):
        return time(15, 30), None


class Calendar:
    def session(self, market, symbol=None):
        return Session()


class BrokenCalendar:
    def session(self, market, symbol=None):
        raise RuntimeError("calendar unavailable")


def at(hour, minute):
    return datetime.combine(date(2026, 9, 21), time(hour, minute), tzinfo=IST)


def book(**quantities):
    exposure = {symbol: Decimal(abs(qty) * 100) for symbol, qty in quantities.items()}
    return PortfolioSnapshot(
        equity=Decimal(100000),
        daily_realized_pnl=Decimal(0),
        gross_exposure=sum(exposure.values(), Decimal(0)),
        symbol_exposure=exposure,
        symbol_quantity=dict(quantities),
    )


MARKS = {"INFY": Decimal(1500), "TCS": Decimal(3200), "RELIANCE": Decimal(1250)}


DEFAULT = object()


def plan(now, portfolio, *, calendar=DEFAULT, marks=None, policy=None):
    # A sentinel rather than None, because "no calendar" is one of the cases under test and
    # must be distinguishable from "the helper's default".
    return plan_session_flatten(
        portfolio,
        market=Market.INDIA,
        now=now,
        calendar=Calendar() if calendar is DEFAULT else calendar,
        marks=MARKS if marks is None else marks,
        policy=policy,
    )


def test_nothing_is_flattened_before_the_cutoff():
    result = plan(at(11, 0), book(INFY=10, TCS=5))

    assert result.verdict == "not_due"
    assert result.orders == ()
    assert result.due is False
    assert "until the flatten window opens" in result.reasons[0]


def test_every_open_position_gets_a_covering_order_once_the_window_opens():
    result = plan(at(15, 20), book(INFY=10, TCS=5))

    assert result.verdict == "ready"
    assert result.due is True
    assert result.covers_everything
    assert {order.symbol for order in result.orders} == {"INFY", "TCS"}
    assert all(order.side is Side.SELL for order in result.orders)
    assert all(order.strategy_id == FLATTEN_STRATEGY_ID for order in result.orders)
    assert sorted(order.quantity for order in result.orders) == [5, 10]


def test_a_short_position_is_covered_by_buying_it_back():
    result = plan(at(15, 20), book(INFY=-7))

    assert [(o.symbol, o.side, o.quantity) for o in result.orders] == [("INFY", Side.BUY, 7)]


def test_a_flatten_order_can_only_ever_reduce_exposure_to_zero():
    # The safety property of the whole module. A covering order that reversed a position, or
    # overshot it, would turn a risk-reducing operation into a new trade nobody decided to
    # make - and this runs without AI approval precisely because it cannot do that.
    long_side = plan(at(15, 20), book(INFY=10)).intents[0]
    short_side = plan(at(15, 20), book(TCS=-4)).intents[0]

    assert long_side.order.side is Side.SELL and long_side.order.quantity == 10
    assert short_side.order.side is Side.BUY and short_side.order.quantity == 4

    with pytest.raises(ValueError, match="must oppose the position"):
        FlattenIntent(order=long_side.order, held_quantity=-10, reason="x")
    with pytest.raises(ValueError, match="must cover the position exactly"):
        FlattenIntent(order=long_side.order, held_quantity=25, reason="x")


def test_an_empty_book_at_the_cutoff_is_already_flat():
    result = plan(at(15, 20), book())

    assert result.verdict == "already_flat"
    assert result.orders == ()
    assert result.covers_everything


def test_a_position_with_no_mark_is_named_rather_than_priced_at_a_guess():
    result = plan(at(15, 20), book(INFY=10, MYSTERY=3))

    assert result.verdict == "incomplete"
    assert result.unpriceable == ("MYSTERY",)
    assert not result.covers_everything
    assert [order.symbol for order in result.orders] == ["INFY"]
    assert "will not be flat until they are" in result.reasons[0]


def test_a_missing_calendar_reports_that_nothing_was_checked():
    # An unarmed control that says "nothing to do" is indistinguishable from an armed one
    # over an empty book, and only one of those is safe.
    result = plan(at(15, 20), book(INFY=10), calendar=None)

    assert result.verdict == "unavailable"
    assert result.orders == ()
    assert not result.covers_everything
    assert "nothing was checked" in result.reasons[0]


def test_a_naive_instant_is_refused_rather_than_assumed():
    # The naive instant is the subject of the test, not an oversight.
    result = plan(datetime(2026, 9, 21, 15, 20), book(INFY=10))  # noqa: DTZ001

    assert result.verdict == "unavailable"
    assert result.as_of is None
    assert "wrong half the year" in result.reasons[0]


def test_a_calendar_that_raises_does_not_pass_the_book_through():
    result = plan(at(15, 20), book(INFY=10), calendar=BrokenCalendar())

    assert result.verdict == "unavailable"
    assert result.orders == ()
    assert "could not be determined" in result.reasons[0]


def test_the_window_opens_outside_the_overnight_firewalls_closing_window():
    # Entries are refused in the last 10 minutes. The flatten starts at 15 so covering
    # orders go out into a session that can no longer add exposure behind them.
    assert FlattenPolicy().cutoff > timedelta(minutes=10)
    # Close 15:30, cutoff 15 minutes, so the window opens at 15:15 exactly.
    assert plan(at(15, 15), book(INFY=1)).verdict == "ready"
    assert plan(at(15, 14), book(INFY=1)).verdict == "not_due"
    # And it opens strictly before the firewall stops entries at 15:20, so covering orders
    # go into a session that can no longer add exposure behind them.
    assert plan(at(15, 20), book(INFY=1)).verdict == "ready"


def test_a_zero_or_negative_cutoff_is_refused():
    with pytest.raises(ValueError, match="positive interval"):
        FlattenPolicy(cutoff=timedelta(0))


# --- proof -----------------------------------------------------------------------------

def test_an_empty_book_with_no_working_orders_is_certified_flat():
    proof = verify_flat(book(), working_orders=(), checked_at=at(15, 35))

    assert proof.flat
    assert proof.verdict == "flat"
    assert not proof.halt_required


def test_a_leftover_position_is_an_unplanned_overnight_and_demands_a_halt():
    proof = verify_flat(book(INFY=10), working_orders=(), checked_at=at(15, 35))

    assert not proof.flat
    assert proof.halt_required
    assert proof.residual == (("INFY", 10),)
    assert "INFY +10" in proof.reasons[0]
    assert any("Halt and alert" in reason for reason in proof.reasons)


def test_an_empty_book_with_a_working_order_is_not_flat():
    # Zero positions with an order still live is a book about to have a position again.
    # Certifying that as flat is how an unplanned overnight trade is logged as a clean close.
    proof = verify_flat(book(), working_orders=("order-1",), checked_at=at(15, 35))

    assert not proof.flat
    assert proof.halt_required
    assert proof.open_positions == 0
    assert proof.open_orders == 1
    assert any("can still acquire a position" in reason for reason in proof.reasons)


def test_evidence_says_a_plan_is_not_proof():
    ready = plan(at(15, 20), book(INFY=10)).as_evidence()

    assert ready["schema"] == "pramana.session_flatten.v1"
    assert ready["orders"][0]["side"] == "BUY" or ready["orders"][0]["side"] == "SELL"
    assert "is not evidence the book is flat" in ready["limitation"]
    assert verify_flat(book(), working_orders=(), checked_at=at(15, 35)).as_evidence()["flat"]
