"""India is one country and several exchanges that do not keep the same hours.

Before this, every India instrument was judged by the NSE cash session (09:15-15:30 IST).
An MCX metal trades until 23:30 in US daylight saving, 23:55 in standard time, so more than half of
gold's session was a window in which the engine held a position and decided nothing about
it. Currency on CDS closes at 17:00, later than equities and earlier than metals.

The tests here are about one thing: the session an instrument is judged by is the session
its exchange actually keeps, and where the platform does not know, it takes the shorter
answer rather than the more permissive one.
"""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from quant_ai.domain.models import Market
from quant_ai.execution.session import (
    INDIA_EXCHANGE_SESSIONS,
    GlobalVenue,
    MarketCalendar,
    MarketState,
    default_holidays,
    holidays_from_json,
    intraday_periods_per_year,
    regular_session_length,
    session_for,
)

IST = ZoneInfo("Asia/Kolkata")
# A Wednesday in US DST, and a Wednesday in US standard time. Neither is an NSE holiday.
DST_DAY = datetime(2026, 9, 16, tzinfo=IST)
STANDARD_DAY = datetime(2026, 1, 14, tzinfo=IST)

BOOK = {"GOLD": "MCX", "USDINR": "CDS", "NIFTY": "NSE"}


def calendar(**kwargs):
    return MarketCalendar(holidays=default_holidays(), exchanges=dict(BOOK), **kwargs)


def at(day, hour, minute=0, *, symbol):
    return calendar().state(Market.INDIA, day.replace(hour=hour, minute=minute), symbol=symbol)


def test_an_mcx_metal_is_open_through_the_evening_the_equity_market_is_closed_for():
    """The eight hours the engine used to be blind to."""
    for hour in (16, 18, 20, 22, 23):
        assert at(DST_DAY, hour, symbol="GOLD") == MarketState.REGULAR_HOURS, hour
        assert at(DST_DAY, hour, symbol="NIFTY") == MarketState.CLOSED, hour
    # And the morning, where they agree.
    assert at(DST_DAY, 10, symbol="GOLD") == at(DST_DAY, 10, symbol="NIFTY") == MarketState.REGULAR_HOURS


def test_currency_closes_at_five_which_is_neither_the_equity_nor_the_metal_close():
    """Three different closes, so no pair of them can stand in for the third."""
    assert at(DST_DAY, 16, symbol="USDINR") == MarketState.REGULAR_HOURS  # equities shut
    assert at(DST_DAY, 18, symbol="USDINR") == MarketState.CLOSED         # metals still open
    assert at(DST_DAY, 18, symbol="GOLD") == MarketState.REGULAR_HOURS


def test_the_mcx_close_follows_us_daylight_saving_and_not_the_indian_calendar():
    """23:30 while New York is on DST, 23:55 once it is not.

    MCX runs its evening session against COMEX, so the close moves with a rule set in a
    different country. Hardcoding either time is wrong for roughly half the year, and the
    half it is wrong for is the half nobody is watching at 23:40.
    """
    assert at(DST_DAY, 23, 40, symbol="GOLD") == MarketState.CLOSED
    assert at(STANDARD_DAY, 23, 40, symbol="GOLD") == MarketState.REGULAR_HOURS
    # The common ground and the far end behave the same either way.
    for day in (DST_DAY, STANDARD_DAY):
        assert at(day, 23, 20, symbol="GOLD") == MarketState.REGULAR_HOURS
        assert at(day, 23, 58, symbol="GOLD") == MarketState.CLOSED


def test_a_metal_has_no_post_market_window_to_mistake_for_trading():
    """POST_MARKET is a real state for equities and not a thing MCX has.

    Equities get 15:30-16:00; the metal goes from open to closed with nothing between. A
    post-market window invented for MCX would be a window in which entries are refused and
    the book still reads as active.
    """
    assert at(DST_DAY, 15, 45, symbol="NIFTY") == MarketState.POST_MARKET
    assert at(STANDARD_DAY, 23, 55, symbol="GOLD") == MarketState.CLOSED
    assert at(DST_DAY, 23, 30, symbol="GOLD") == MarketState.CLOSED


def test_an_unmapped_symbol_takes_the_shorter_session_rather_than_the_longer_one():
    """The direction the unknown case must fail in.

    An instrument the operator never mapped, or an exchange code this build does not know,
    resolves to the venue session. That is the shortest Indian one, so the engine stops
    early on something it should have kept trading - it never decides into a closed book,
    which is the error that costs money rather than opportunity.
    """
    assert at(DST_DAY, 20, symbol="UNLISTED") == MarketState.CLOSED
    assert session_for(Market.INDIA, "NOT_AN_EXCHANGE") is session_for(Market.INDIA, None)
    assert session_for(Market.INDIA, None).regular_close == time(15, 30)


def test_an_explicit_exchange_beats_the_symbol_map():
    """The scheduler holds an Instrument and passes its exchange directly."""
    book = calendar()
    evening = DST_DAY.replace(hour=20)
    assert book.state(Market.INDIA, evening, exchange="MCX") == MarketState.REGULAR_HOURS
    # Same symbol, contradicted by an explicit code: the explicit one wins.
    assert book.state(Market.INDIA, evening, exchange="NSE", symbol="GOLD") == MarketState.CLOSED


def test_a_weekend_is_closed_on_every_exchange_however_long_its_weekday_session():
    saturday = datetime(2026, 9, 19, 20, tzinfo=IST)
    for symbol in BOOK:
        assert calendar().state(Market.INDIA, saturday, symbol=symbol) == MarketState.CLOSED


def test_an_exchange_without_its_own_holidays_inherits_the_national_calendar():
    """Inheriting costs sessions; the opposite would trade into a closed book."""
    republic_day = datetime(2026, 1, 26, 20, tzinfo=IST)
    assert calendar().state(Market.INDIA, republic_day, symbol="GOLD") == MarketState.CLOSED


def test_an_operator_can_state_the_days_one_exchange_keeps_and_another_does_not():
    """The escape hatch for MCX's own calendar, which is not NSE's.

    Keyed on the exchange, so naming an MCX holiday does not close NSE and vice versa.
    """
    holidays = holidays_from_json({"MCX": ["2026-09-16"]}, default_holidays())
    book = MarketCalendar(holidays=holidays, exchanges=dict(BOOK))
    evening = DST_DAY.replace(hour=20)
    midday = DST_DAY.replace(hour=11)
    assert book.state(Market.INDIA, evening, symbol="GOLD") == MarketState.CLOSED
    assert book.state(Market.INDIA, midday, symbol="NIFTY") == MarketState.REGULAR_HOURS
    # The venue list still applies to the exchanges that did not override it.
    assert GlobalVenue.INDIA in holidays and "MCX" in holidays


def test_a_ratio_is_annualised_against_the_session_its_instrument_actually_trades():
    """Fixed normal-session annualisation follows each venue's standard-time close.

    This is not an actual-day/session count and does not establish strategy skill.
    """
    minute = timedelta(minutes=1)
    assert regular_session_length(Market.INDIA, "NSE") == timedelta(hours=6, minutes=15)
    assert regular_session_length(Market.INDIA, "MCX") == timedelta(hours=14, minutes=55)
    assert regular_session_length(Market.INDIA, "CDS") == timedelta(hours=8)
    assert intraday_periods_per_year(Market.INDIA, minute, "NSE") == 94500
    assert intraday_periods_per_year(Market.INDIA, minute, "MCX") == 895 * 252
    # Unchanged for callers that pass no exchange, so nothing silently re-scales.
    assert intraday_periods_per_year(Market.INDIA, minute) == 94500


def test_the_us_venue_is_untouched_by_any_of_this():
    usa = MarketCalendar(holidays=default_holidays(), exchanges={"AAPL": "NASDAQ"})
    ny = datetime(2026, 9, 16, 11, tzinfo=ZoneInfo("America/New_York"))
    assert usa.state(Market.USA, ny, symbol="AAPL") == MarketState.REGULAR_HOURS
    assert usa.state(Market.USA, ny.replace(hour=20)) == MarketState.CLOSED


@pytest.mark.parametrize("code", sorted(INDIA_EXCHANGE_SESSIONS))
def test_every_shipped_session_opens_before_it_closes_and_names_a_real_zone(code):
    session = INDIA_EXCHANGE_SESSIONS[code]
    assert session.timezone == "Asia/Kolkata"
    assert session.pre_open <= session.regular_open < session.regular_close <= session.post_close
    if session.us_dst_regular_close is not None:
        # Seasonal variants may be shorter or longer but cannot invert the session.
        assert session.regular_open < session.us_dst_regular_close
        assert session.us_dst_regular_close <= (session.us_dst_post_close or time(23, 59))
