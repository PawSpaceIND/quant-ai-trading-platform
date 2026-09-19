"""The exchange sessions reaching the parts of the engine that act on them.

``test_exchange_sessions`` covers what the calendar answers. This covers whether anything
asks it: a correct calendar nothing consults is the same blind engine with better
bookkeeping. Three consumers decide whether an MCX metal trades in its evening session -
the cadence scheduler, the entry gate, and the overnight firewall - and each one is here.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from quant_ai.domain.models import (
    AssetClass,
    Instrument,
    Market,
    OrderIntent,
    PortfolioSnapshot,
    Side,
)
from quant_ai.execution.scheduler import AutonomousCadenceScheduler
from quant_ai.execution.session import MarketCalendar, MarketState, default_holidays
from quant_ai.risk.overnight import OvernightExposureFirewall, OvernightRiskPolicy

IST = ZoneInfo("Asia/Kolkata")
# 20:00 IST on a Wednesday: MCX is trading, NSE closed for four and a half hours.
EVENING = datetime(2026, 9, 16, 20, tzinfo=IST).astimezone(timezone.utc)
# A tradable MCX row is a contract, not a listing: MCX gold is 100 grams a lot on a
# INR 1 tick, and it dies on a date. December 2026 is comfortably ahead of every
# timestamp these tests pin, so none of them sit in a rollover window by accident.
GOLD = Instrument(
    "GOLD", Market.INDIA, AssetClass.METAL, "INR", "MCX",
    expiry=date(2026, 12, 5), lot_size=100, tick_size=Decimal(1), underlying="GOLD",
)
NIFTY = Instrument("NIFTY", Market.INDIA, AssetClass.INDEX, "INR", "NSE")
BOOK = {"GOLD": "MCX", "NIFTY": "NSE"}
EQUITY = Decimal(1_000_000)


def book_calendar() -> MarketCalendar:
    return MarketCalendar(holidays=default_holidays(), exchanges=dict(BOOK))


def order(symbol: str = "GOLD") -> OrderIntent:
    return OrderIntent(
        symbol, Market.INDIA, Side.BUY, 1, Decimal(100), "test", AssetClass.METAL,
        stop_price=Decimal(97), take_profit_price=Decimal(106),
    )


class Reached(Exception):
    """Raised by the stub pipeline to prove a tick got past the session gate."""


class StubPipeline:
    """Enough pipeline for the scheduler, and nothing that could pass by accident."""

    def __init__(self) -> None:
        self.macro = SimpleNamespace(fetch=lambda *a, **k: SimpleNamespace(indicators=()))
        self.news = SimpleNamespace(fetch=lambda *a, **k: ())

    def run(self, *args, **kwargs):
        raise Reached


def test_the_cadence_scheduler_runs_the_pipeline_for_a_metal_in_its_evening_session():
    """The gate that skipped the whole swarm, and the reason gold went unanalysed.

    ``run_tick`` returns an off-hours brief without ever calling the pipeline when the
    state is not REGULAR_HOURS. Judged by NSE hours that was every evening tick. The stub
    raises the moment the pipeline is reached, so reaching it is the assertion - a brief
    that merely came back would not distinguish the two paths.
    """
    scheduler = AutonomousCadenceScheduler(StubPipeline(), calendar=book_calendar())
    portfolio = PortfolioSnapshot(EQUITY, Decimal(0), Decimal(0), EQUITY)

    with pytest.raises(Reached):
        scheduler.run_tick(GOLD, EVENING, None, portfolio, country="INDIA")

    # Same instant, same scheduler: the equity index is genuinely shut and never gets there.
    brief = scheduler.run_tick(NIFTY, EVENING, None, portfolio, country="INDIA")
    assert brief.market_state == MarketState.CLOSED
    assert scheduler.last_result is None


def test_the_overnight_firewall_no_longer_refuses_every_evening_entry_as_out_of_session():
    """``overnight_entry_outside_session`` was the second gate, behind the first.

    The firewall sees an OrderIntent, which carries no exchange, so it resolves the
    session through the calendar's symbol map. Without that map an MCX entry at 20:00 is
    refused for being outside a session it is squarely inside.
    """
    policy = OvernightRiskPolicy(max_overnight_gross=Decimal("0.25"), refuse_outside_session=True)
    firewall = OvernightExposureFirewall(policy, calendar=book_calendar())
    portfolio = PortfolioSnapshot(EQUITY, Decimal(0), Decimal(0), EQUITY)

    approved = firewall.evaluate(order("GOLD"), portfolio, EVENING)
    assert approved.approved, approved.reason

    # An NSE symbol at the same instant is genuinely outside its session and still refused.
    refused = firewall.evaluate(order("NIFTY"), portfolio, EVENING)
    assert not refused.approved
    assert refused.reason == "overnight_entry_outside_session"


def test_the_closing_window_is_measured_against_the_close_the_instrument_actually_has():
    """A window anchored to 15:30 for a book that shuts at 23:30 is never in force.

    The window refuses entries in the final minutes before the close, so that a position
    is not opened with no time left to manage it. Anchored to the wrong close it either
    fires in the middle of the afternoon or, for a metal, never fires at all.
    """
    policy = OvernightRiskPolicy(
        max_overnight_gross=Decimal("0.25"),
        refuse_outside_session=False,
        closing_window=timedelta(minutes=30),
    )
    firewall = OvernightExposureFirewall(policy, calendar=book_calendar())
    portfolio = PortfolioSnapshot(EQUITY, Decimal(0), Decimal(0), EQUITY)

    # 23:20 IST, inside MCX's final half hour under the DST close of 23:30.
    late = datetime(2026, 9, 16, 23, 20, tzinfo=IST).astimezone(timezone.utc)
    assert firewall.evaluate(order("GOLD"), portfolio, late).reason == "overnight_closing_window"
    # 20:00 is mid-session for the metal, so the window must not be in force.
    assert firewall.evaluate(order("GOLD"), portfolio, EVENING).approved


def test_an_unmapped_calendar_still_refuses_the_evening_so_the_map_is_what_opens_it():
    """The control for every assertion above: the wiring, not a change in defaults.

    A calendar built without the watchlist map judges GOLD by NSE hours and refuses, which
    is what the engine did everywhere before. Nothing here got more permissive on its own;
    the operator's stated venue is what changes the answer.
    """
    policy = OvernightRiskPolicy(max_overnight_gross=Decimal("0.25"), refuse_outside_session=True)
    blind = OvernightExposureFirewall(policy, calendar=MarketCalendar(holidays=default_holidays()))
    portfolio = PortfolioSnapshot(EQUITY, Decimal(0), Decimal(0), EQUITY)
    decision = blind.evaluate(order("GOLD"), portfolio, EVENING)
    assert not decision.approved
    assert decision.reason == "overnight_entry_outside_session"


def test_teaching_calendar_about_mcx_alone_does_not_admit_it_to_pilot():
    """MCX hours are not admission without verified fee and margin evidence.

    The pilot can now admit a fully bound MCX contract, but the session map alone is never
    permission. This fixture supplies no verified derivative economics, so admission still
    refuses it.
    """
    from quant_ai.governance.pilot import validate_pilot_instruments

    with pytest.raises(ValueError, match="pilot_mcx_fee_schedule_unverified"):
        validate_pilot_instruments((GOLD,))
    with pytest.raises(ValueError, match="not_supported"):
        validate_pilot_instruments((NIFTY,))  # an index is not cash equity either
    # The one shape the pilot does accept, unchanged.
    validate_pilot_instruments(
        (Instrument("INFY", Market.INDIA, AssetClass.EQUITY, "INR", "NSE"),)
    )


# A watched row: the same metal, named as a listing rather than a contract. It carries no
# expiry or lot because nothing can be ordered against it, which is the point of the flag.
WATCHED_GOLD = Instrument(
    "GOLD", Market.INDIA, AssetClass.METAL, "INR", "MCX", tradable=False,
)
INFY = Instrument("INFY", Market.INDIA, AssetClass.EQUITY, "INR", "NSE")
# 02:00 IST: MCX is shut too, so this pins the gate ahead of the session branch rather
# than a session that happens to be closed.
DEAD_OF_NIGHT = datetime(2026, 9, 17, 2, tzinfo=IST).astimezone(timezone.utc)


class ObservationPipeline(StubPipeline):
    """``run`` is still a landmine; the feed is what an observation is allowed to read."""

    def __init__(self, candles=()) -> None:
        super().__init__()
        self.candles = list(candles)
        self.market_feed = SimpleNamespace(fetch_ohlcv=lambda *a, **k: list(self.candles))

    async def run_async(self, *args, **kwargs):
        raise Reached


def candle(close: str):
    return SimpleNamespace(timestamp=EVENING, close=Decimal(close))


@pytest.mark.parametrize("moment", [EVENING, DEAD_OF_NIGHT])
def test_a_watched_metal_is_read_without_entering_the_execution_path(moment):
    """Observation is what was asked for and trading is not: the account holds no MCX
    segment, so a proposal against gold could only ever be rejected by the broker. The gate
    sits ahead of the session branch, so the row never reaches the pipeline in any state."""
    scheduler = AutonomousCadenceScheduler(
        ObservationPipeline([candle("153585")]), calendar=book_calendar()
    )

    brief = scheduler.run_tick(
        WATCHED_GOLD, moment, plan=None, portfolio=None, country="INDIA",
    )

    assert brief.mode == "OBSERVATION_ONLY"
    assert brief.risk_decision == "observation_only_instrument_not_tradable"
    assert brief.paper_order_ids == ()
    assert "last_price=153585" in brief.swarm_consensus
    assert scheduler.last_result is None


def test_a_watched_metal_reports_the_mcx_session_it_was_read_in():
    scheduler = AutonomousCadenceScheduler(
        ObservationPipeline([candle("153585")]), calendar=book_calendar()
    )

    brief = scheduler.run_tick(
        WATCHED_GOLD, EVENING, plan=None, portfolio=None, country="INDIA",
    )

    # 20:00 IST: the equity clock says CLOSED, and judging gold by it is the blindness
    # this whole path exists to remove.
    assert brief.market_state is MarketState.REGULAR_HOURS
    assert "MCX_session=REGULAR_HOURS" in brief.provider_status


def test_a_watched_metal_with_no_bars_says_so_rather_than_printing_a_price():
    scheduler = AutonomousCadenceScheduler(ObservationPipeline(), calendar=book_calendar())

    brief = scheduler.run_tick(
        WATCHED_GOLD, EVENING, plan=None, portfolio=None, country="INDIA",
    )

    assert "last_price=unavailable" in brief.swarm_consensus
    assert "bars=0" in brief.provider_status


def test_the_pilot_admits_a_watched_row_beside_the_cash_book():
    from quant_ai.governance.pilot import validate_pilot_instruments

    validate_pilot_instruments((INFY, WATCHED_GOLD))


def test_the_pilot_refuses_a_watched_row_in_the_primary_seat():
    """instruments[0] is the daemon's primary - the name whose country charges the
    allocation cap and whose brief leads. An observation row cannot hold that seat."""
    from quant_ai.governance.pilot import validate_pilot_instruments

    with pytest.raises(ValueError, match="pilot_primary_instrument_must_be_tradable"):
        validate_pilot_instruments((WATCHED_GOLD, INFY))
