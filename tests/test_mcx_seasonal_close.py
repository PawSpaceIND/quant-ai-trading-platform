"""MCX non-agricultural normal closes from the exchange, not prior test assumptions.

Source: https://www.mcxindia.com/market-operations/trading-surveillance
Normal 09:00–23:30; 23:55 extension typically November–March. Holiday/special
sessions remain separate and are not inferred by these normal-weekday cases.
"""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from quant_ai.domain.models import Market
from quant_ai.execution.session import MarketCalendar, MarketState, session_for

IST = ZoneInfo("Asia/Kolkata")
DAYS = [(date(2026, 1, 14), time(23, 55)), (date(2026, 7, 15), time(23, 30)),
        (date(2026, 9, 16), time(23, 30)), (date(2026, 12, 16), time(23, 55))]


@pytest.mark.parametrize("day,close", DAYS)
@pytest.mark.parametrize("microseconds,state", [(-1, MarketState.REGULAR_HOURS),
                                               (0, MarketState.CLOSED),
                                               (1, MarketState.CLOSED)])
def test_published_mcx_normal_close_has_an_exact_exclusive_boundary(day, close, microseconds, state):
    instant = datetime.combine(day, close, tzinfo=IST) + timedelta(microseconds=microseconds)
    calendar = MarketCalendar(holidays={}, special_sessions={}, exchanges={"SYNTHETIC": "MCX"})
    assert calendar.state(Market.INDIA, instant, symbol="SYNTHETIC") == state
    assert calendar.state(Market.INDIA, instant.astimezone(timezone.utc), exchange="MCX") == state


@pytest.mark.parametrize("day,close", DAYS)
def test_mcx_regular_and_post_close_are_the_same_in_both_seasons(day, close):
    session = session_for(Market.INDIA, "MCX")
    assert session.closes_on(day) == (close, close)


@pytest.mark.parametrize("day,close", DAYS)
def test_mcx_season_does_not_change_nse_cash_or_currency_hours(day, close):
    calendar = MarketCalendar(holidays={}, special_sessions={})
    evening = datetime.combine(day, time(23, 40), tzinfo=IST)
    for venue in ("NSE", "BSE", "NFO", "BFO", "CDS", "BCD", "NCDEX"):
        assert calendar.state(Market.INDIA, evening, exchange=venue) is MarketState.CLOSED
    assert session_for(Market.INDIA, "NSE").closes_on(day) == (time(15, 30), time(16))


def test_mcx_standard_time_normalization_uses_actual_standard_close():
    from quant_ai.execution.session import intraday_periods_per_year, regular_session_length
    assert regular_session_length(Market.INDIA, "MCX") == timedelta(hours=14, minutes=55)
    assert intraday_periods_per_year(Market.INDIA, timedelta(minutes=1), "MCX") == 895 * 252


@pytest.mark.parametrize("day,close", DAYS)
def test_overnight_firewall_uses_correct_seasonal_closing_window(day, close):
    from decimal import Decimal

    from test_exchange_session_wiring import EQUITY, book_calendar, order

    from quant_ai.domain.models import PortfolioSnapshot
    from quant_ai.risk.overnight import OvernightExposureFirewall, OvernightRiskPolicy
    policy = OvernightRiskPolicy(max_overnight_gross=Decimal("0.25"),
                                 refuse_outside_session=False,
                                 closing_window=timedelta(minutes=30))
    firewall = OvernightExposureFirewall(policy, calendar=book_calendar())
    portfolio = PortfolioSnapshot(EQUITY, Decimal(0), Decimal(0), EQUITY)
    actual_close = datetime.combine(day, close, tzinfo=IST)
    assert firewall.evaluate(order("GOLD"), portfolio, actual_close-timedelta(minutes=15)).reason == "overnight_closing_window"
    assert firewall.evaluate(order("GOLD"), portfolio, actual_close-timedelta(minutes=35)).reason == "approved_overnight_risk"
