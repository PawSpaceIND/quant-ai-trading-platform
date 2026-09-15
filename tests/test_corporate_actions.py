"""A corporate action re-bases the quote; it must never be read as a stop breach.

A 1:5 split cuts the quoted price to a fifth while the held quantity and the stored cost
basis stand. Acting on that comparison liquidates the whole position and books a loss of
perhaps eighty percent that the market never caused, which can then trip the drawdown
breaker and halt the pilot. These tests cover both layers: the ex-dates an operator
declares, and the step guard that catches the ones nobody declared.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from test_pilot_closure import publish_tick, runner_for

from quant_ai.domain.models import Market, OrderIntent, Side
from quant_ai.marketdata.corporate_calendar import (
    SCHEMA,
    CorporateActionCalendar,
    ScheduledAction,
    price_discontinuity,
)

SESSION = datetime(2026, 9, 15, 6, 0, tzinfo=timezone.utc)   # 11:30 IST, a trading Tuesday


# ----------------------------------------------------------------- the calendar itself

def test_a_declared_ex_date_is_found_on_its_ist_session_date():
    from datetime import date

    calendar = CorporateActionCalendar((ScheduledAction("INFY", date(2026, 9, 15), "split_1_5"),))

    assert calendar.action_on("INFY", SESSION) == "split_1_5"
    assert calendar.action_on("infy", SESSION) == "split_1_5", "symbols are matched case-insensitively"
    assert calendar.action_on("TCS", SESSION) is None
    assert calendar.action_on("INFY", SESSION + timedelta(days=1)) is None


def test_the_session_date_is_indian_not_utc():
    """20:00 UTC is already the next day in IST; the calendar must follow the exchange."""
    from datetime import date

    calendar = CorporateActionCalendar((ScheduledAction("INFY", date(2026, 9, 16), "bonus"),))
    late = datetime(2026, 9, 15, 20, 0, tzinfo=timezone.utc)   # 01:30 IST on the 16th

    assert calendar.action_on("INFY", late) == "bonus"


def test_a_naive_timestamp_is_refused():
    calendar = CorporateActionCalendar()
    with pytest.raises(ValueError):
        calendar.action_on("INFY", SESSION.replace(tzinfo=None))


def test_a_malformed_calendar_yields_no_declared_actions(tmp_path):
    bad = tmp_path / "actions.json"
    for payload in ('{ not json', json.dumps({"schema": "wrong", "actions": []}),
                    json.dumps({"schema": SCHEMA, "actions": "not-a-list"}),
                    json.dumps({"schema": SCHEMA, "actions": [{"symbol": "", "ex_date": "2026-09-15"}]})):
        bad.write_text(payload, encoding="utf-8")
        assert len(CorporateActionCalendar.from_file(bad)) == 0

    assert len(CorporateActionCalendar.from_file(tmp_path / "absent.json")) == 0
    assert len(CorporateActionCalendar.from_file(None)) == 0


def test_a_good_calendar_loads(tmp_path):
    path = tmp_path / "actions.json"
    path.write_text(json.dumps({"schema": SCHEMA, "actions": [
        {"symbol": "INFY", "ex_date": "2026-09-15", "kind": "split_1_5"},
        {"symbol": "TCS", "ex_date": "2026-10-01", "kind": "dividend"},
    ]}), encoding="utf-8")

    calendar = CorporateActionCalendar.from_file(path)

    assert len(calendar) == 2
    assert calendar.action_on("INFY", SESSION) == "split_1_5"


# ----------------------------------------------------------------- the step guard

def test_the_step_guard_separates_a_re_based_quote_from_a_market_move():
    # A limit-hit move is real trading and must stay stoppable.
    assert not price_discontinuity(Decimal(100), Decimal(81))     # -19%
    assert not price_discontinuity(Decimal(100), Decimal(119))    # +19%
    # A 1:5 split, a 1:1 bonus and a consolidation are not trading.
    assert price_discontinuity(Decimal(1000), Decimal(200))
    assert price_discontinuity(Decimal(1000), Decimal(500))
    assert price_discontinuity(Decimal(100), Decimal(1000))


def test_the_step_guard_never_raises_and_never_fires_on_the_first_look():
    assert not price_discontinuity(None, Decimal(100)), "nothing to compare with yet"
    assert not price_discontinuity(Decimal(0), Decimal(100))
    assert not price_discontinuity(Decimal(100), Decimal(0))
    assert not price_discontinuity(Decimal(-5), Decimal(100))


# ----------------------------------------------------------------- the engine behaviour

def at(daemon, moment):
    daemon.clock = lambda: moment
    return moment


def feed(runner, price, moment):
    at(runner.daemon, moment)
    publish_tick(runner, price, moment)


def sweep(runner, moment):
    at(runner.daemon, moment)
    runner.daemon.protection_tick(moment)


def protected_position(runner, moment=SESSION):
    feed(runner, "1000", moment)
    runner.daemon.tracker.broker.buy(
        OrderIntent("INFY", Market.INDIA, Side.BUY, 5, Decimal(1000), "test",
                    tenant_id=runner.daemon.tenant_id,
                    stop_price=Decimal(950), take_profit_price=Decimal(1200))
    )


def test_a_split_does_not_liquidate_the_position(tmp_path):
    runner = runner_for(tmp_path)
    daemon = runner.daemon
    protected_position(runner)
    sweep(runner, SESSION)                       # establishes the reference mark
    assert daemon.tracker.broker.get_positions(daemon.tenant_id)

    # 1:5 split: the quote is now a fifth, far below the 950 stop.
    after = SESSION + timedelta(minutes=1)
    feed(runner, "200", after)
    sweep(runner, after)

    assert daemon.exit_engine.rebased == ("INFY",)
    assert daemon.tracker.broker.get_positions(daemon.tenant_id), \
        "a re-based quote must never book a fabricated loss"
    assert not any(item.filled for item in daemon.protective_exits)


def test_a_real_breach_still_liquidates(tmp_path):
    """The guard must not become a blanket excuse to ignore stops."""
    runner = runner_for(tmp_path)
    daemon = runner.daemon
    protected_position(runner)
    sweep(runner, SESSION)

    after = SESSION + timedelta(minutes=1)
    feed(runner, "940", after)                   # -6%, through the 950 stop, well inside the band
    sweep(runner, after)

    assert daemon.exit_engine.rebased == ()
    assert any(item.filled for item in daemon.protective_exits)
    assert not daemon.tracker.broker.get_positions(daemon.tenant_id)


def test_a_declared_ex_date_suspends_the_exit_even_without_a_big_step(tmp_path):
    from datetime import date

    runner = runner_for(tmp_path)
    daemon = runner.daemon
    daemon.exit_engine.corporate_calendar = CorporateActionCalendar(
        (ScheduledAction("INFY", date(2026, 9, 15), "dividend"),)
    )
    protected_position(runner)
    sweep(runner, SESSION)

    after = SESSION + timedelta(minutes=1)
    feed(runner, "940", after)                   # a small move, but it is the ex-date
    sweep(runner, after)

    assert daemon.exit_engine.rebased == ("INFY",)
    assert daemon.tracker.broker.get_positions(daemon.tenant_id)
