"""The overnight gap: the hole it leaves, the disambiguation, and the exposure limits.

The first two tests are the evidence for the problem rather than for the fix. A stop is
swept against live ticks and can only act on a price that traded; a position carried
through a close reopens wherever the next session decides, and nothing in the engine had
an opinion about that. Everything after them is the policy.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from test_pilot_closure import publish_tick, runner_for

from quant_ai.agents.swarm import TradeProposal
from quant_ai.brokers.adapter import BrokerPosition
from quant_ai.domain.models import (
    AssetClass,
    Market,
    OrderIntent,
    PortfolioSnapshot,
    RiskMode,
    Side,
)
from quant_ai.execution.overnight import (
    DEFAULT_ESCALATION_INTERVAL,
    OvernightGapMonitor,
    crossed_session_boundary,
)
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.execution.protective_exits import ProtectiveExitEngine
from quant_ai.execution.session import GlobalVenue, MarketCalendar, regular_session_length
from quant_ai.marketdata.gap import (
    DEFAULT_BAND_FRACTION,
    GapVerdict,
    classify_gap,
    nearest_action_ratio,
)
from quant_ai.notifications.trading import TradingAlertCode, TradingNotificationDispatcher
from quant_ai.planning.capital import CapitalGoalEngine, CapitalPlanRequest
from quant_ai.risk.overnight import OvernightExposureFirewall, OvernightRiskPolicy
from quant_ai.risk.warden import RiskWarden

EQUITY = Decimal(100000)
# NSE trades 09:15-15:30 IST, which is 03:45-10:00 UTC.
MORNING = datetime(2026, 9, 15, 4, 0, tzinfo=timezone.utc)
AFTERNOON = datetime(2026, 9, 15, 9, 0, tzinfo=timezone.utc)
NEXT_MORNING = datetime(2026, 9, 16, 4, 0, tzinfo=timezone.utc)
# Deliberately tz-naive: where an instant sits relative to a close is unanswerable without
# an offset, and both halves of the policy must decline rather than assume one.
NAIVE_MORNING = MORNING.replace(tzinfo=None)
NAIVE_AFTERNOON = AFTERNOON.replace(tzinfo=None)


def snapshot(gross: Decimal = Decimal(0), **kwargs) -> PortfolioSnapshot:
    return PortfolioSnapshot(EQUITY, Decimal(0), gross, EQUITY, **kwargs)


def entry(
    symbol: str = "INFY",
    price: Decimal = Decimal(100),
    quantity: int = 100,
    market: Market = Market.INDIA,
) -> OrderIntent:
    return OrderIntent(
        symbol, market, Side.BUY, quantity, price, "test", AssetClass.EQUITY,
        stop_price=price * Decimal("0.97"), take_profit_price=price * Decimal("1.06"),
    )


def proposal(symbol: str = "INFY", price: Decimal = Decimal(100), quantity: int = 100):
    return TradeProposal(
        "decision-1", symbol, Market.INDIA, "INDIA", AssetClass.EQUITY, Side.BUY, quantity,
        price, price * Decimal("0.97"), price * Decimal("1.06"),
        Decimal("0.8"), Decimal("0.05"), Decimal("0.02"), ("test",),
    )


def plan():
    return CapitalGoalEngine().recommend(
        CapitalPlanRequest(EQUITY, Decimal("0.8"), Decimal("0.2"), requested_mode=RiskMode.BALANCED)
    )


def position(symbol: str = "INFY", stop: Decimal = Decimal(95)) -> BrokerPosition:
    return BrokerPosition(
        "t", symbol, Market.INDIA, AssetClass.EQUITY, 100, Decimal(100),
        stop_price=stop, take_profit_price=Decimal(120),
    )


def engine_over(marks, broker, monitor=None) -> ProtectiveExitEngine:
    """An exit engine reading each sweep's mark from ``marks`` by symbol."""
    return ProtectiveExitEngine(
        broker, lambda item: marks[item.symbol], tenant_id="t", gap_monitor=monitor
    )


class StubBroker:
    """The two calls the exit engine makes, without a ledger behind them."""

    def __init__(self, positions) -> None:
        self.positions = list(positions)
        self.sold = []

    def get_protection_positions(self, tenant_id):
        return tuple(self.positions)

    def sell_protected(self, order, proof, cooldown_until):
        self.sold.append(order)
        self.positions = [item for item in self.positions if item.symbol != order.symbol]
        return type("Fill", (), {"order_id": f"order-{len(self.sold)}"})()


# --------------------------------------------------------------------------- the problem


def test_a_stop_does_not_survive_the_gap_it_was_sized_for() -> None:
    """A 3% stop on a position carried overnight pays whatever the open decides.

    The sizer solved for a loss of three points. The engine sweeps live ticks, so it can
    only act on a price that traded, and the first price of the next session is 85. The
    stop still fires - this step is inside the exchange band, so the re-based-quote guard
    never sees it - but it fires five times beyond the risk the position was sized to
    take, and there was no sweep in between at which it could have done better.
    """
    broker = StubBroker([position(stop=Decimal(97))])
    marks = {"INFY": Decimal(100)}
    engine = engine_over(marks, broker)
    assert engine.evaluate(AFTERNOON) == (), "the close leaves the position open"

    marks["INFY"] = Decimal(85)
    exits = engine.evaluate(NEXT_MORNING)
    assert len(exits) == 1
    assert exits[0].mark_price == Decimal(85)
    intended = (Decimal(100) - Decimal(97)) * 100
    assert exits[0].realized_pnl == Decimal(-1500)
    assert -exits[0].realized_pnl == intended * 5, "the gap is paid in full, not at the stop"


def test_an_undeclared_discontinuity_holds_the_stop_until_the_basis_agrees_again() -> None:
    """The suspension lasts as long as the quote and the cost basis disagree.

    ``price_discontinuity`` compares each mark with the last one that was *comparable* with
    the basis, so the stored mark is deliberately not advanced while a step is being
    refused. Advancing it would erase the evidence: the next sweep would compare 70 with
    70, find no step, and liquidate against the pre-adjustment basis - the fabricated loss
    this guard exists to prevent, one sweep later.

    Which way this fails is a real choice and it is made here. A split and a crash of the
    same size are indistinguishable to a size test, so holding leaves a genuinely
    collapsing position unprotected until an operator acts. That is the safer direction on
    a step across a session boundary, where an exchange price band makes a corporate action
    far likelier than a real move, and it is why a step *inside* one session re-arms the
    stop instead. The gap monitor is what puts the open question in front of a human.
    """
    broker = StubBroker([position(stop=Decimal(95))])
    marks = {"INFY": Decimal(100)}
    engine = engine_over(marks, broker)
    engine.evaluate(AFTERNOON)

    marks["INFY"] = Decimal(70)
    moment = NEXT_MORNING
    for sweep in range(5):
        assert engine.evaluate(moment) == (), f"sweep {sweep} must not liquidate"
        assert engine.rebased == ("INFY",)
        assert broker.positions, "the position is still held"
        moment += timedelta(seconds=1)


def test_a_reconciled_position_leaves_the_suspension_behind() -> None:
    """The hold is on the disagreement, not on the symbol.

    Once the quote is comparable with the basis again - an operator having re-based the
    position, or the price simply having come back - the stop arms itself without anyone
    clearing a flag, and a later breach is acted on normally.
    """
    broker = StubBroker([position(stop=Decimal(95))])
    marks = {"INFY": Decimal(100)}
    engine = engine_over(marks, broker)
    engine.evaluate(AFTERNOON)

    marks["INFY"] = Decimal(70)
    assert engine.evaluate(NEXT_MORNING) == ()
    assert engine.rebased == ("INFY",)

    marks["INFY"] = Decimal(99)  # back inside the band of the last comparable mark
    assert engine.evaluate(NEXT_MORNING + timedelta(seconds=1)) == ()
    assert engine.rebased == (), "the quote and the basis agree again"

    marks["INFY"] = Decimal(94)  # an ordinary move through the stop
    exits = engine.evaluate(NEXT_MORNING + timedelta(seconds=2))
    assert [item.mark_price for item in exits] == [Decimal(94)]


def test_the_engine_has_no_opinion_about_carrying_a_position_through_a_close() -> None:
    """Unarmed, an entry minutes before the close is judged exactly as one at the open.

    This is the state the policy changes, recorded so the change is visible. The warden's
    caps are notional buckets with no session term in them at all.
    """
    warden = RiskWarden()
    assert warden.overnight_risk.armed is False
    at_open = warden.evaluate(proposal(quantity=40), plan(), snapshot(), now=MORNING)
    at_close = warden.evaluate(
        proposal(quantity=40), plan(), snapshot(), now=MORNING.replace(hour=9, minute=55)
    )
    assert at_open.approved and at_close.approved
    assert at_open.reason == at_close.reason == "approved"


# ------------------------------------------------------------------- the disambiguation


@pytest.mark.parametrize(
    ("previous", "current", "label"),
    [
        (Decimal(1500), Decimal(300), "1:5"),  # a 1:5 split
        (Decimal(100), Decimal(50), "1:2"),  # a 1:1 bonus
        (Decimal(100), Decimal(500), "5:1"),  # a 5:1 consolidation
    ],
)
def test_a_step_on_an_exact_action_ratio_is_reported_but_never_believed(
    previous, current, label
) -> None:
    """Roundness travels with the verdict as evidence; it does not settle the verdict."""
    assessment = classify_gap(previous, current, crossed_session_boundary=True)
    assert assessment.nearest_action == label
    assert assessment.verdict is GapVerdict.UNDETERMINED
    assert not assessment.resolved, "an undeclared action is still an open question"


def test_a_failed_result_and_a_one_for_five_split_are_the_same_step() -> None:
    """The reason a size threshold cannot do this job, stated as a test.

    A company that loses four fifths of its value and a 1:5 split both take the quote to
    a fifth of where it was. Anything that reads 0.8 as a corporate action silences the
    stop on the first; anything that reads it as a crash liquidates the second against a
    cost basis that no longer means anything.
    """
    split = classify_gap(Decimal(1500), Decimal(300), crossed_session_boundary=True)
    collapse = classify_gap(Decimal(100), Decimal(20), crossed_session_boundary=True)
    assert split.step_fraction == collapse.step_fraction == Decimal("0.8")
    assert split.nearest_action == collapse.nearest_action == "1:5"
    assert split.verdict is collapse.verdict is GapVerdict.UNDETERMINED


def test_a_declared_ex_date_is_the_only_thing_that_settles_a_discontinuity() -> None:
    declared = classify_gap(
        Decimal(1500), Decimal(300), crossed_session_boundary=True, declared_action="split_1_5"
    )
    assert declared.verdict is GapVerdict.DECLARED_ACTION
    assert declared.resolved


def test_a_large_step_inside_one_session_cannot_be_a_corporate_action() -> None:
    """An action re-bases at an open, so two marks in one session cannot straddle one."""
    assessment = classify_gap(Decimal(100), Decimal(70), crossed_session_boundary=False)
    assert assessment.verdict is GapVerdict.INTRASESSION_BREAK
    assert not assessment.resolved


def test_a_limit_hit_move_inside_the_band_stays_an_ordinary_move() -> None:
    assessment = classify_gap(Decimal(100), Decimal(81), crossed_session_boundary=True)
    assert assessment.verdict is GapVerdict.ORDINARY
    assert assessment.resolved
    assert assessment.step_fraction == Decimal("0.19") < DEFAULT_BAND_FRACTION


def test_a_crash_that_lands_off_every_action_ratio_carries_no_roundness_evidence() -> None:
    assessment = classify_gap(Decimal(100), Decimal("63.7"), crossed_session_boundary=True)
    assert assessment.verdict is GapVerdict.UNDETERMINED
    assert assessment.nearest_action == "", "0.637 is no small-integer action ratio"


def test_roundness_tolerance_is_tight_enough_to_mean_something() -> None:
    """A split that opens two percent off theoretical is not claimed as one."""
    assert nearest_action_ratio(Decimal("0.2")) == "1:5"
    assert nearest_action_ratio(Decimal("0.204")) == ""


def test_an_unusable_input_reads_as_ordinary_rather_than_as_a_gap() -> None:
    """A missing or broken previous mark leaves every protective level as it was.

    Reading a first observation as a discontinuity would raise an alarm on every symbol
    at every restart, which is how a loud channel becomes one nobody reads.
    """
    for previous, current in ((None, Decimal(100)), (Decimal(0), Decimal(100)),
                              (Decimal(100), Decimal(-1)), (Decimal(100), None)):
        assessment = classify_gap(previous, current, crossed_session_boundary=True)
        assert assessment.verdict is GapVerdict.ORDINARY
        assert assessment.resolved


def test_the_classifier_never_raises_on_a_nonsensical_band() -> None:
    assert classify_gap(
        Decimal(100), Decimal(20), crossed_session_boundary=True, band=Decimal(0)
    ).verdict is GapVerdict.ORDINARY


# ---------------------------------------------------------------------- session boundary


def test_the_boundary_is_the_venues_own_close_not_a_utc_day() -> None:
    india = GlobalVenue.INDIA
    assert not crossed_session_boundary(MORNING, AFTERNOON, india)
    assert crossed_session_boundary(AFTERNOON, NEXT_MORNING, india)
    # 15:00 and 15:20 IST are 09:30 and 09:50 UTC: one session, despite the UTC hour.
    assert not crossed_session_boundary(
        datetime(2026, 9, 15, 9, 30, tzinfo=timezone.utc),
        datetime(2026, 9, 15, 9, 50, tzinfo=timezone.utc),
        india,
    )


# --------------------------------------------------------------------------- escalation


def monitor_for(**kwargs) -> tuple[OvernightGapMonitor, TradingNotificationDispatcher]:
    dispatcher = TradingNotificationDispatcher()
    return OvernightGapMonitor(dispatcher=dispatcher, tenant_id="t", **kwargs), dispatcher


def gap_alerts(dispatcher, tenant_id: str = "t") -> tuple:
    return tuple(
        item for item in dispatcher.pending(tenant_id)
        if item.code is TradingAlertCode.OVERNIGHT_GAP_UNEXPLAINED
    )


def test_an_unexplained_overnight_step_reaches_the_operator_immediately() -> None:
    monitor, dispatcher = monitor_for()
    monitor.observe("INFY", Market.INDIA, Decimal(100), AFTERNOON)
    assert gap_alerts(dispatcher) == (), "one observation is not yet a step"

    monitor.observe("INFY", Market.INDIA, Decimal(70), NEXT_MORNING)
    alerts = gap_alerts(dispatcher)
    assert len(alerts) == 1
    assert alerts[0].metadata["verdict"] == GapVerdict.UNDETERMINED.value
    assert alerts[0].metadata["resolved"] == "False"
    assert monitor.unresolved == ("INFY",)


def test_an_unresolved_suspension_cannot_stay_silent_for_a_session() -> None:
    """The escalation cadence is short enough that a session cannot pass without alerts.

    Thirty minutes is ``ProtectiveExitEngine.re_entry_cooldown``: an unexplained price is
    repeated at least as often as the engine would let itself re-enter a stopped-out
    symbol. Against the shortest regular session in ``SESSIONS`` that is twelve alerts.
    """
    monitor, dispatcher = monitor_for()
    monitor.observe("INFY", Market.INDIA, Decimal(100), AFTERNOON)
    monitor.observe("INFY", Market.INDIA, Decimal(70), NEXT_MORNING)

    # A second of ordinary trading later: still open, and not yet repeated.
    monitor.observe("INFY", Market.INDIA, Decimal(70), NEXT_MORNING + timedelta(seconds=1))
    assert len(gap_alerts(dispatcher)) == 1

    moment = NEXT_MORNING
    session = regular_session_length(GlobalVenue.INDIA)
    while moment - NEXT_MORNING < session:
        moment += DEFAULT_ESCALATION_INTERVAL
        monitor.observe("INFY", Market.INDIA, Decimal(70), moment)
    expected = session // DEFAULT_ESCALATION_INTERVAL
    assert len(gap_alerts(dispatcher)) >= expected >= 12
    assert monitor.unresolved == ("INFY",)


def test_ordinary_trading_afterwards_does_not_explain_the_gap_away() -> None:
    """The gap happened. A quiet tick a second later is not an account of it."""
    monitor, _ = monitor_for()
    monitor.observe("INFY", Market.INDIA, Decimal(100), AFTERNOON)
    monitor.observe("INFY", Market.INDIA, Decimal(70), NEXT_MORNING)
    monitor.observe("INFY", Market.INDIA, Decimal("70.1"), NEXT_MORNING + timedelta(minutes=1))
    assert monitor.unresolved == ("INFY",)


def test_the_operators_declaration_closes_the_loop_and_says_so() -> None:
    declared = {}
    monitor, dispatcher = monitor_for(
        declared_action_lookup=lambda symbol, moment: declared.get(symbol)
    )
    monitor.observe("INFY", Market.INDIA, Decimal(1500), AFTERNOON)
    monitor.observe("INFY", Market.INDIA, Decimal(300), NEXT_MORNING)
    assert monitor.unresolved == ("INFY",)

    declared["INFY"] = "split_1_5"
    monitor.observe("INFY", Market.INDIA, Decimal(301), NEXT_MORNING + timedelta(minutes=1))
    assert monitor.unresolved == ()
    assert gap_alerts(dispatcher)[-1].metadata["resolved"] == "True"


def test_an_open_step_can_be_read_off_a_page_and_not_only_off_an_alert() -> None:
    """The alert reaches whoever was watching a dispatcher. The page is for everyone else.

    Same facts, same exactness: marks stay text, and the deadline is the one
    ``halt_reason`` will actually act on rather than a second opinion about it.
    """
    monitor, _ = monitor_for()
    assert monitor.unresolved_state() == ()
    monitor.observe("INFY", Market.INDIA, Decimal(100), AFTERNOON)
    monitor.observe("INFY", Market.INDIA, Decimal(70), NEXT_MORNING)
    (row,) = monitor.unresolved_state()
    assert row["symbol"] == "INFY"
    assert row["verdict"] == GapVerdict.UNDETERMINED.value
    assert row["previousMark"] == "100" and row["currentMark"] == "70"
    assert row["firstSeenAt"] == NEXT_MORNING.isoformat()
    halts_at = NEXT_MORNING + regular_session_length(GlobalVenue.INDIA)
    assert row["haltsAt"] == halts_at.isoformat()
    assert monitor.halt_reason(halts_at) == "overnight_gap_unresolved:INFY"
    # An operator's declaration removes it from the page as well as from the halt clock.
    monitor.forget(())
    assert monitor.unresolved_state() == ()


def test_an_unresolved_step_halts_after_one_full_session_and_not_before() -> None:
    monitor, _ = monitor_for()
    monitor.observe("INFY", Market.INDIA, Decimal(100), AFTERNOON)
    monitor.observe("INFY", Market.INDIA, Decimal(70), NEXT_MORNING)
    session = regular_session_length(GlobalVenue.INDIA)
    assert monitor.halt_reason(NEXT_MORNING + session - timedelta(minutes=1)) is None
    assert monitor.halt_reason(NEXT_MORNING + session) == "overnight_gap_unresolved:INFY"


def test_a_closed_position_stops_escalating() -> None:
    monitor, _ = monitor_for()
    monitor.observe("INFY", Market.INDIA, Decimal(100), AFTERNOON)
    monitor.observe("INFY", Market.INDIA, Decimal(70), NEXT_MORNING)
    monitor.forget(())
    assert monitor.unresolved == ()
    assert monitor.halt_reason(NEXT_MORNING + timedelta(days=7)) is None


def test_the_monitor_declines_to_judge_what_it_cannot_and_never_raises() -> None:
    """A naive clock, a venue with no session and a raising operator source all abstain."""
    monitor, dispatcher = monitor_for(
        declared_action_lookup=lambda symbol, moment: (_ for _ in ()).throw(RuntimeError("x"))
    )
    assert monitor.observe("INFY", Market.INDIA, Decimal(100), NAIVE_MORNING) is None
    assert monitor.observe("XAU", Market.GLOBAL, Decimal(100), MORNING) is None
    # The raising lookup must not stop the step being classified and reported.
    monitor.observe("INFY", Market.INDIA, Decimal(100), AFTERNOON)
    monitor.observe("INFY", Market.INDIA, Decimal(70), NEXT_MORNING)
    assert monitor.unresolved == ("INFY",)
    assert len(gap_alerts(dispatcher)) == 1


def test_a_raising_dispatcher_cannot_break_the_sweep() -> None:
    class Broken(TradingNotificationDispatcher):
        def dispatch(self, *args, **kwargs):
            raise RuntimeError("sink down")

    monitor = OvernightGapMonitor(dispatcher=Broken(), tenant_id="t")
    monitor.observe("INFY", Market.INDIA, Decimal(100), AFTERNOON)
    assert monitor.observe("INFY", Market.INDIA, Decimal(70), NEXT_MORNING) is not None
    assert monitor.unresolved == ("INFY",)


# ------------------------------------------------------- the monitor never suppresses


def test_an_ambiguous_overnight_step_stays_suspended_but_stops_being_silent() -> None:
    """Where a split and a crash are indistinguishable, the suspension stands - loudly.

    The monitor does not second-guess the guard here. It cannot tell the two apart either,
    and liquidating against a cost basis that may no longer mean anything is the
    fabricated loss the guard exists to prevent. What it changes is that the step becomes
    an open question with an operator's name on it and a deadline, instead of a log line
    nothing reads - and the operator is told *before* the next sweep decides anything.
    """
    monitor, dispatcher = monitor_for()
    broker = StubBroker([position(stop=Decimal(95))])
    marks = {"INFY": Decimal(100)}
    engine = engine_over(marks, broker, monitor)
    engine.evaluate(AFTERNOON)

    marks["INFY"] = Decimal(70)
    assert engine.evaluate(NEXT_MORNING) == ()
    assert engine.rebased == ("INFY",), "the guard's suspension is untouched"
    assert gap_alerts(dispatcher), "but the operator now hears about it"
    assert monitor.unresolved == ("INFY",)
    session = regular_session_length(GlobalVenue.INDIA)
    assert monitor.halt_reason(NEXT_MORNING + session) == "overnight_gap_unresolved:INFY"


def test_a_step_that_cannot_be_a_corporate_action_keeps_its_stop() -> None:
    """The narrowing, and the only one: a split cannot happen between two intraday marks.

    A corporate action re-bases at an open. A -30% step between two marks five minutes
    apart inside one session is therefore not one, and the quote still refers to the same
    unit the stored stop does. The size test alone suspends it anyway, which leaves a
    genuinely collapsing position naked; with the classifier the stop fires.
    """
    monitor, dispatcher = monitor_for()
    broker = StubBroker([position(stop=Decimal(95))])
    marks = {"INFY": Decimal(100)}
    engine = engine_over(marks, broker, monitor)
    engine.evaluate(AFTERNOON)

    marks["INFY"] = Decimal(70)
    intrasession = AFTERNOON + timedelta(minutes=5)
    exits = engine.evaluate(intrasession)
    assert [item.symbol for item in exits] == ["INFY"], "the stop fires"
    assert engine.rebased == ()
    assert gap_alerts(dispatcher), "and the operator is still told the step was abnormal"


def test_the_monitor_never_creates_a_suspension_the_guard_would_not_have() -> None:
    """It may re-arm a stop; it may never switch one off.

    An ordinary breach inside the exchange band never reaches the re-based-quote guard, so
    the monitor must have no effect on it at all - on the same book, same marks, with and
    without one.
    """
    marks = {"INFY": Decimal(100)}
    monitor, _ = monitor_for()
    with_monitor = StubBroker([position(stop=Decimal(95))])
    without = StubBroker([position(stop=Decimal(95))])
    watched, plain = engine_over(marks, with_monitor, monitor), engine_over(marks, without)
    watched.evaluate(AFTERNOON)
    plain.evaluate(AFTERNOON)

    marks["INFY"] = Decimal(90)
    assert len(watched.evaluate(NEXT_MORNING)) == len(plain.evaluate(NEXT_MORNING)) == 1
    assert watched.rebased == plain.rebased == ()


def test_a_declared_ex_date_suspends_without_ever_becoming_an_open_question() -> None:
    """The operator said it was a split, so there is nothing for them to resolve."""
    declared = {"INFY": "split_1_5"}
    monitor, dispatcher = monitor_for(
        declared_action_lookup=lambda symbol, moment: declared.get(symbol)
    )

    class Calendar:
        def action_on(self, symbol, moment):
            return declared.get(symbol)

    broker = StubBroker([position(stop=Decimal(95))])
    marks = {"INFY": Decimal(100)}
    engine = ProtectiveExitEngine(
        broker, lambda item: marks[item.symbol], tenant_id="t",
        corporate_calendar=Calendar(), gap_monitor=monitor,
    )
    engine.evaluate(AFTERNOON)
    marks["INFY"] = Decimal(20)
    assert engine.evaluate(NEXT_MORNING) == (), "no fabricated loss is booked"
    assert engine.rebased == ("INFY",)
    assert monitor.unresolved == (), "a declaration is not an open question"
    assert monitor.halt_reason(NEXT_MORNING + timedelta(days=7)) is None
    assert gap_alerts(dispatcher) == (), "and nobody is paged about an expected event"


# ------------------------------------------------------------------ the exposure limits


def armed(policy: OvernightRiskPolicy | None = None) -> OvernightExposureFirewall:
    return OvernightExposureFirewall(policy, calendar=MarketCalendar())


def test_the_overnight_gross_cap_is_the_book_stress_tolerance_over_the_worst_gap() -> None:
    """2% of equity, the intraday loss breaker, divided by the -8% crisis gap.

    Five positions at the 5% single-trade cap are exactly 25%, and five is the default
    ``max_open_positions``: the cap permits the book the engine was built to hold and
    refuses anything past it.
    """
    from quant_ai.intelligence.adversarial import AdversarialStressAgent
    from quant_ai.risk.policy import RiskPolicy

    worst = min(item.equity_shock for item in AdversarialStressAgent.DEFAULT_SCENARIOS)
    tolerance = AdversarialStressAgent().book_tolerance_fraction
    assert tolerance == RiskPolicy().max_daily_loss == Decimal("0.02")
    assert tolerance / -worst == OvernightRiskPolicy().max_overnight_gross == Decimal("0.25")
    assert Decimal(5) * RiskPolicy().max_single_trade_notional == Decimal("0.25")


def test_the_cap_only_ever_subtracts_from_the_limits_that_already_existed() -> None:
    from quant_ai.risk.policy import BookRiskPolicy, RiskPolicy

    cap = OvernightRiskPolicy().max_overnight_gross
    assert cap < BookRiskPolicy().max_correlation_adjusted_gross
    assert cap < RiskPolicy().max_gross_exposure


def test_a_book_over_the_overnight_cap_is_refused_while_the_notional_cap_allows_it() -> None:
    firewall = armed()
    # 24% held plus a 4% entry: inside the 60% notional gross cap, past the 25% overnight.
    book = snapshot(Decimal(24000), symbol_exposure={"TCS": Decimal(24000)})
    decision = firewall.evaluate(entry(price=Decimal(100), quantity=40), book, AFTERNOON)
    assert decision == type(decision)(False, "overnight_gross_limit")

    smaller = firewall.evaluate(entry(price=Decimal(100), quantity=10), book, AFTERNOON)
    assert smaller.approved and smaller.reason == "approved_overnight_risk"


def test_an_entry_inside_the_closing_window_is_refused_rather_than_sized_down() -> None:
    """One cadence interval before the close the swarm gets no second look at it."""
    firewall = armed()
    # 15:25 IST is 09:55 UTC, five minutes inside the ten-minute window.
    late = datetime(2026, 9, 15, 9, 55, tzinfo=timezone.utc)
    assert firewall.evaluate(entry(), snapshot(), late).reason == "overnight_closing_window"
    # 15:19 IST is outside it.
    early = datetime(2026, 9, 15, 9, 49, tzinfo=timezone.utc)
    assert firewall.evaluate(entry(), snapshot(), early).approved


def test_an_entry_outside_the_regular_session_is_refused() -> None:
    firewall = armed()
    closed = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)  # 17:30 IST
    assert firewall.evaluate(entry(), snapshot(), closed).reason == "overnight_entry_outside_session"


def test_an_operator_may_keep_only_the_gross_cap_and_still_gets_the_gross_cap() -> None:
    """Relaxing the session rule must not relax the exposure rule with it."""
    lenient = armed(OvernightRiskPolicy(refuse_outside_session=False))
    closed = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    assert lenient.evaluate(entry(quantity=10), snapshot(), closed).approved
    book = snapshot(Decimal(24000), symbol_exposure={"TCS": Decimal(24000)})
    assert lenient.evaluate(entry(quantity=40), book, closed).reason == "overnight_gross_limit"


@pytest.mark.parametrize(
    ("order", "moment", "reason"),
    [
        (entry(), None, "overnight_risk_unavailable:no_timestamp"),
        (entry(), NAIVE_AFTERNOON, "overnight_risk_unavailable:no_timestamp"),
        (entry(market=Market.GLOBAL), AFTERNOON, "overnight_risk_unavailable:unknown_venue"),
    ],
)
def test_a_policy_that_cannot_be_evaluated_refuses_exposure(order, moment, reason) -> None:
    """Armed, every unanswerable question is a refusal and never a pass."""
    decision = armed().evaluate(order, snapshot(), moment)
    assert not decision.approved
    assert decision.reason == reason


def test_a_zero_equity_book_refuses_rather_than_dividing_by_it() -> None:
    empty = PortfolioSnapshot(Decimal(0), Decimal(0), Decimal(0))
    assert armed().evaluate(entry(), empty, AFTERNOON).reason == (
        "overnight_risk_unavailable:invalid_equity"
    )


def test_a_raising_calendar_is_a_refusal_and_never_an_exception() -> None:
    class Broken(MarketCalendar):
        def state(self, market, timestamp):
            raise RuntimeError("calendar down")

    firewall = OvernightExposureFirewall(calendar=Broken())
    decision = firewall.evaluate(entry(), snapshot(), AFTERNOON)
    assert decision.reason == "overnight_risk_unavailable:RuntimeError"
    assert not decision.approved


def test_an_unarmed_firewall_approves_everything_it_is_shown() -> None:
    firewall = OvernightExposureFirewall()
    assert not firewall.armed
    for moment in (None, AFTERNOON, datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)):
        decision = firewall.evaluate(entry(), snapshot(Decimal(55000)), moment)
        assert decision.approved and decision.reason == "overnight_risk_not_armed"


def test_a_nonsensical_policy_is_refused_at_construction() -> None:
    for bad in (Decimal(0), Decimal("-0.1"), Decimal("1.5")):
        with pytest.raises(ValueError):
            OvernightRiskPolicy(max_overnight_gross=bad)
    with pytest.raises(ValueError):
        OvernightRiskPolicy(closing_window=timedelta(minutes=-1))


# ------------------------------------------------------------------- through the warden


def warden_armed() -> RiskWarden:
    return RiskWarden(overnight_risk=armed())


def test_the_warden_refuses_an_entry_the_overnight_cap_will_not_carry() -> None:
    book = snapshot(Decimal(24000), symbol_exposure={"TCS": Decimal(24000)})
    decision = warden_armed().evaluate(proposal(quantity=40), plan(), book, now=AFTERNOON)
    assert not decision.approved
    assert decision.reason == "overnight_gross_limit"


def test_the_warden_reports_the_overnight_gate_among_its_armed_inputs() -> None:
    assert "overnight_session_calendar" in warden_armed().book_risk_armed
    assert "overnight_session_calendar" not in RiskWarden().book_risk_armed
    assert warden_armed().overnight_risk_policy.max_overnight_gross == Decimal("0.25")


def test_an_unarmed_warden_leaves_the_entry_path_exactly_as_it_was() -> None:
    """The 1100 tests that predate this must be judged by the limits they were written for."""
    plain = RiskWarden()
    book = snapshot(Decimal(24000), symbol_exposure={"TCS": Decimal(24000)})
    for moment in (None, AFTERNOON, datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)):
        decision = plain.evaluate(proposal(quantity=40), plan(), book, now=moment)
        assert decision.approved and decision.reason == "approved"


def test_an_exit_is_never_trapped_by_an_overnight_limit(tmp_path) -> None:
    """A covered SELL is approved before any of this runs, at any hour, on any book.

    A halt freezes risk-taking, not exits, and an overnight policy that could refuse a
    liquidation would be the exact failure it exists to prevent.
    """
    PaperBrokerService(str(tmp_path / "ledger.db"))
    held = PortfolioSnapshot(
        EQUITY, Decimal(0), Decimal(80000), EQUITY,
        symbol_exposure={"INFY": Decimal(80000)}, symbol_quantity={"INFY": 800},
    )
    exit_proposal = TradeProposal(
        "decision-2", "INFY", Market.INDIA, "INDIA", AssetClass.EQUITY, Side.SELL, 800,
        Decimal(100), None, None, Decimal("0.9"), Decimal(0), Decimal(0), ("exit",),
    )
    midnight = datetime(2026, 9, 15, 20, 0, tzinfo=timezone.utc)
    decision = warden_armed().evaluate(exit_proposal, plan(), held, now=midnight)
    assert decision.approved, "a covered exit passes even outside the session"


# ----------------------------------------------------------------- through the daemon


def ghost(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    return runner_for(tmp_path)


def held_position(runner, price, moment, *, stop, target) -> None:
    runner.daemon.clock = lambda: moment
    publish_tick(runner, price, moment)
    runner.daemon.tracker.broker.buy(
        OrderIntent("INFY", Market.INDIA, Side.BUY, 5, Decimal(price), "test",
                    tenant_id=runner.daemon.tenant_id,
                    stop_price=Decimal(stop), take_profit_price=Decimal(target))
    )


def daemon_sweep(runner, price, moment) -> None:
    runner.daemon.clock = lambda: moment
    publish_tick(runner, price, moment)
    runner.daemon.protection_tick(moment)


def test_the_daemon_arms_the_gap_monitor_only_when_the_operator_asks(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("PRAMANA_OVERNIGHT_GAP_MONITOR", raising=False)
    assert ghost(tmp_path / "off").daemon.exit_engine.gap_monitor is None
    monkeypatch.setenv("PRAMANA_OVERNIGHT_GAP_MONITOR", "session")
    assert ghost(tmp_path / "on").daemon.exit_engine.gap_monitor is not None
    monkeypatch.setenv("PRAMANA_OVERNIGHT_GAP_MONITOR", "sometimes")
    with pytest.raises(RuntimeError):
        ghost(tmp_path / "bad")


def test_a_gap_the_operator_never_explains_stops_new_risk_after_one_session(
    tmp_path, monkeypatch
) -> None:
    """A step that clears the band but not the stop leaves the position sitting there.

    The stop is not breached, so nothing liquidates and nothing would otherwise have said
    a word. The monitor pages the operator at the open and keeps paging; a full trading
    session later, with the discontinuity still unaccounted for, the daemon stops adding
    risk. It does not liquidate: the engine has just said it cannot interpret this quote,
    and selling against it would book whichever loss the ambiguity invented.
    """
    monkeypatch.setenv("PRAMANA_OVERNIGHT_GAP_MONITOR", "session")
    runner = ghost(tmp_path)
    daemon = runner.daemon
    held_position(runner, "100", AFTERNOON, stop=70, target=200)
    daemon_sweep(runner, "100", AFTERNOON)
    assert daemon.exit_engine.gap_monitor.unresolved == ()

    daemon_sweep(runner, "74", NEXT_MORNING)
    assert daemon.exit_engine.gap_monitor.unresolved == ("INFY",)
    assert daemon.tracker.broker.get_positions(daemon.tenant_id), "nothing was liquidated"
    assert not daemon.kill_switch.engaged, "one unexplained step is not yet a halt"
    assert gap_alerts(daemon.notifications, "pilot"), "the operator hears about it at once"

    session = regular_session_length(GlobalVenue.INDIA)
    daemon_sweep(runner, "74", NEXT_MORNING + session)
    assert daemon.kill_switch.engaged
    assert daemon.kill_switch.reason == "overnight_gap_unresolved:INFY"


def test_the_overnight_cap_arms_from_the_environment_and_never_from_a_guess(
    tmp_path, monkeypatch
) -> None:
    def warden_of(runner):
        return runner.daemon.scheduler.pipeline.runtime.warden

    monkeypatch.delenv("PRAMANA_OVERNIGHT_GROSS_CAP", raising=False)
    assert not warden_of(ghost(tmp_path / "off")).overnight_risk.armed

    monkeypatch.setenv("PRAMANA_OVERNIGHT_GROSS_CAP", "0.25")
    armed_warden = warden_of(ghost(tmp_path / "on"))
    assert armed_warden.overnight_risk.armed
    assert armed_warden.overnight_risk_policy.max_overnight_gross == Decimal("0.25")

    # A cap the operator typed but the engine cannot read is a boot failure. Starting
    # unarmed would be the one outcome they did not ask for.
    monkeypatch.setenv("PRAMANA_OVERNIGHT_GROSS_CAP", "a quarter")
    with pytest.raises(RuntimeError):
        ghost(tmp_path / "bad")


def test_arming_the_overnight_gate_never_approves_what_it_would_have_refused() -> None:
    """The monotonicity check the cross-position gates are held to, applied here.

    Over every combination of held book, entry size and moment in the session, an armed
    warden may turn an approval into a refusal and never the other way round.
    """
    plain, gated = RiskWarden(), warden_armed()
    moments = (
        MORNING,
        AFTERNOON,
        datetime(2026, 9, 15, 9, 55, tzinfo=timezone.utc),  # inside the closing window
        datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc),  # after the close
        datetime(2026, 9, 20, 6, 0, tzinfo=timezone.utc),  # a Sunday
        None,
    )
    capital = plan()
    for gross in (Decimal(0), Decimal(10000), Decimal(24000), Decimal(40000)):
        book = snapshot(gross, symbol_exposure={"TCS": gross} if gross else {})
        for quantity in (1, 10, 40, 49):
            for moment in moments:
                before = plain.evaluate(proposal(quantity=quantity), capital, book, now=moment)
                after = gated.evaluate(proposal(quantity=quantity), capital, book, now=moment)
                if after.approved:
                    assert before.approved, (
                        f"the overnight gate approved what the plain warden refused "
                        f"({before.reason}) at gross={gross} qty={quantity} now={moment}"
                    )


def test_an_unarmed_daemon_sweeps_a_gap_exactly_as_it_always_did(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("PRAMANA_OVERNIGHT_GAP_MONITOR", raising=False)
    runner = ghost(tmp_path)
    daemon = runner.daemon
    held_position(runner, "100", AFTERNOON, stop=70, target=200)
    daemon_sweep(runner, "100", AFTERNOON)
    daemon_sweep(runner, "74", NEXT_MORNING + regular_session_length(GlobalVenue.INDIA))
    assert not daemon.kill_switch.engaged
    assert gap_alerts(daemon.notifications, "pilot") == ()
