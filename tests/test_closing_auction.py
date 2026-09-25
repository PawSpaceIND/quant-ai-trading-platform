"""The closing auction is not a feed outage.

On 24 and 25 September 2026 the pilot latched ``protection_unreachable:COALINDIA`` at
15:24 IST, both times on a healthy feed. Since 3 August 2026 NSE and BSE run a closing
auction in F&O stocks: continuous trading stops at 15:15, orders are collected from 15:20
and matched at 15:30. The auction's book shows bids above asks, the tick buffer refuses
every such tick as crossed, and the held name's last accepted price aged out 120 s after
15:20. These tests replay that and pin what must still halt.
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from test_pilot_closure import publish_tick, runner_for

from quant_ai.domain.models import AssetClass, Instrument, Market, OrderIntent, Side
from quant_ai.execution.session import MarketCalendar, default_holidays, in_closing_auction
from quant_ai.marketdata.ticker_stream import LiveTick

IST = timezone(timedelta(hours=5, minutes=30))
CALENDAR = MarketCalendar(holidays=default_holidays())
STOCK = Instrument("COALINDIA", Market.INDIA, AssetClass.EQUITY, "INR", "NSE")


def ist(day: int, hour: int, minute: int, second: int = 0, month: int = 9) -> datetime:
    return datetime(2026, month, day, hour, minute, second, tzinfo=IST)


# ------------------------------------------------------------------ the window itself


def test_the_auction_runs_from_1515_to_the_close_on_a_trading_day():
    assert not in_closing_auction(CALENDAR, STOCK, ist(25, 15, 14, 59))
    assert in_closing_auction(CALENDAR, STOCK, ist(25, 15, 15))
    assert in_closing_auction(CALENDAR, STOCK, ist(25, 15, 20, 2))
    assert in_closing_auction(CALENDAR, STOCK, ist(25, 15, 29, 59))
    assert not in_closing_auction(CALENDAR, STOCK, ist(25, 15, 30)), "after the close the market is closed"
    assert not in_closing_auction(CALENDAR, STOCK, ist(25, 11, 0))


def test_bse_cash_equities_run_the_same_auction():
    assert in_closing_auction(CALENDAR, replace(STOCK, exchange="BSE"), ist(25, 15, 20))
    assert in_closing_auction(CALENDAR, replace(STOCK, exchange=" nse "), ist(25, 15, 20))


def test_no_auction_before_it_began_on_3_august_2026():
    assert not in_closing_auction(CALENDAR, STOCK, ist(31, 15, 20, month=7))
    assert in_closing_auction(CALENDAR, STOCK, ist(3, 15, 20, month=8))


def test_no_auction_on_a_holiday_or_a_weekend():
    assert not in_closing_auction(CALENDAR, STOCK, ist(2, 15, 20, month=10)), "Gandhi Jayanti"
    assert not in_closing_auction(CALENDAR, STOCK, ist(26, 15, 20)), "Saturday"


def test_etfs_derivatives_and_other_markets_trade_on():
    """Phase 1 is F&O stocks. An ETF trades continuously to 15:30, and so on."""
    for other in (
        Instrument("GOLDBEES", Market.INDIA, AssetClass.ETF, "INR", "NSE"),
        # Only the fields the rule reads: a real derivative also carries its contract.
        SimpleNamespace(symbol="GOLD", market=Market.INDIA, asset_class=AssetClass.EQUITY, exchange="MCX"),
        SimpleNamespace(symbol="COALINDIA", market=Market.INDIA, asset_class=AssetClass.FUTURE, exchange="NFO"),
        Instrument("AAPL", Market.USA, AssetClass.EQUITY, "USD", "NASDAQ"),
    ):
        assert not in_closing_auction(CALENDAR, other, ist(25, 15, 20)), other.symbol


# ------------------------------------------------------------ the protection halt


def at(daemon, moment):
    daemon.clock = lambda: moment
    return moment


def sweep(runner, moment):
    at(runner.daemon, moment)
    runner.daemon.protection_tick(moment)


def crossed(runner, moment):
    """An auction tick: the book shows a bid above the ask, so the buffer refuses it."""
    at(runner.daemon, moment)
    buffer = runner.daemon.tracker.market_feed.buffer
    accepted = buffer.put(LiveTick("INFY", Decimal(100), Decimal(100), Decimal("100.5"), Decimal(100), moment, "test"))
    assert accepted is False


# 25 September 2026: COALINDIA's last accepted tick, as 15:20 order entry began.
LAST_GOOD_TICK = ist(25, 15, 20, 2)


def held_into_the_auction(runner, last_tick=LAST_GOOD_TICK):
    """One INFY position with a 95 stop whose last accepted tick is at ``last_tick``."""
    at(runner.daemon, last_tick)
    publish_tick(runner, "100", last_tick)
    runner.daemon.tracker.broker.buy(
        OrderIntent("INFY", Market.INDIA, Side.BUY, 5, Decimal(100), "test",
                    tenant_id=runner.daemon.tenant_id,
                    stop_price=Decimal(95), take_profit_price=Decimal(120))
    )


def test_25_september_replayed_the_auction_does_not_halt(tmp_path):
    runner = runner_for(tmp_path)
    daemon = runner.daemon
    held_into_the_auction(runner)
    moment = ist(25, 15, 20, 5)
    while moment < ist(25, 15, 30):
        crossed(runner, moment)
        sweep(runner, moment)
        moment += timedelta(seconds=30)
    assert daemon.tracker.market_feed.buffer.integrity()["rejected"]["crossed_tick_quotes"] > 0
    assert daemon.exit_engine.unprotected == ("INFY",), "the sweep still says it cannot price"
    assert "INFY" in daemon.unprotected_since, "the panel still shows how long it has been"
    assert "INFY" not in daemon.unpriced_in_session_since, "the halt clock is paused"
    assert not daemon.kill_switch.engaged, daemon.kill_switch.reason


def test_a_feed_still_dead_at_the_next_open_halts_as_before(tmp_path):
    runner = runner_for(tmp_path)
    daemon = runner.daemon
    held_into_the_auction(runner)
    grace = timedelta(seconds=daemon.unprotected_halt_seconds)
    sweep(runner, ist(25, 15, 24, 3))
    sweep(runner, ist(25, 20, 0))

    opening = ist(29, 9, 15)  # Monday
    sweep(runner, opening)
    sweep(runner, opening + grace - timedelta(seconds=1))
    assert not daemon.kill_switch.engaged, "the auction and the night must not count toward the grace"

    sweep(runner, opening + grace)
    assert daemon.kill_switch.reason == "protection_unreachable:INFY"


def test_the_halt_clock_runs_until_continuous_trading_ends(tmp_path):
    runner = runner_for(tmp_path)
    daemon = runner.daemon
    held_into_the_auction(runner, ist(25, 15, 5))
    grace = timedelta(seconds=daemon.unprotected_halt_seconds)

    first = ist(25, 15, 10)
    sweep(runner, first)
    assert daemon.unpriced_in_session_since == {"INFY": first}
    sweep(runner, first + grace)
    assert daemon.kill_switch.reason == "protection_unreachable:INFY", "15:10-15:12 is continuous trading"


def test_an_etf_held_into_the_auction_window_still_halts(tmp_path):
    runner = runner_for(tmp_path)
    daemon = runner.daemon
    held_into_the_auction(runner)
    first = ist(25, 15, 23)
    sweep(runner, first)
    assert not daemon.kill_switch.engaged and daemon.exit_engine.unprotected == ("INFY",)
    # Only the reachability check runs from here, so no other control can latch first.
    daemon.instruments = tuple(replace(item, asset_class=AssetClass.ETF) for item in daemon.instruments)

    daemon._check_protection_reachable(first)
    daemon._check_protection_reachable(first + timedelta(seconds=daemon.unprotected_halt_seconds))
    assert daemon.kill_switch.reason == "protection_unreachable:INFY", "an ETF trades continuously to 15:30"


def test_a_position_the_engine_has_no_instrument_for_is_still_counted_in_the_auction(tmp_path):
    runner = runner_for(tmp_path)
    daemon = runner.daemon
    held_into_the_auction(runner)
    first = ist(25, 15, 23)
    sweep(runner, first)
    assert not daemon.kill_switch.engaged and daemon.exit_engine.unprotected == ("INFY",)
    daemon.instruments = tuple(replace(item, symbol="TCS") for item in daemon.instruments)
    daemon._check_protection_reachable(first)
    daemon._check_protection_reachable(first + timedelta(seconds=daemon.unprotected_halt_seconds))
    assert daemon.kill_switch.reason == "protection_unreachable:INFY"
    assert daemon.prices_expected("INFY", first) is True
    assert daemon.closing_auction("INFY", first) is False, "an unconfigured name is never in an auction"


# ------------------------------------------------------------------- the panel


def published(runner):
    row = runner.daemon.tracker.broker._connection.execute("SELECT payload FROM pilot_runtime").fetchone()
    return json.loads(row[0])["protectionSweep"]["unprotected"]


def test_the_panel_is_told_the_clock_is_paused_for_the_auction(tmp_path):
    runner = runner_for(tmp_path)
    held_into_the_auction(runner)
    auction = ist(25, 15, 24, 3)
    sweep(runner, auction)

    assert published(runner) == [{"symbol": "INFY", "unpricedSince": auction.isoformat(),
                                  "pricesExpected": False, "closingAuction": True,
                                  "haltClockSince": None}]


def test_after_the_close_the_row_carries_no_auction_flag(tmp_path):
    runner = runner_for(tmp_path)
    held_into_the_auction(runner)
    evening = ist(25, 17, 58)
    sweep(runner, evening)

    assert published(runner) == [{"symbol": "INFY", "unpricedSince": evening.isoformat(),
                                  "pricesExpected": False, "haltClockSince": None}]


def test_an_unreadable_auction_answer_is_left_out_and_the_publish_goes_on(tmp_path):
    runner = runner_for(tmp_path)
    daemon = runner.daemon
    held_into_the_auction(runner)
    sweep(runner, ist(25, 15, 24))

    def unreadable(symbol, now):
        raise ValueError("calendar unavailable")
    daemon.closing_auction = unreadable
    daemon.telemetry.publish(ist(25, 15, 25))

    row = published(runner)[0]
    assert row["pricesExpected"] is False
    assert "closingAuction" not in row
