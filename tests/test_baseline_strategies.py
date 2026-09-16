"""The number the swarm has to beat, and the guards that stop it being a flattering one.

Before this there was no baseline at all, so "the AI decided X" could not be judged: there
was nothing to judge it against and no honest arithmetic to judge it with. These tests are
about four ways a baseline can lie, and the machinery that prevents each:

* **Lookahead.** A rule that reads the close of the bar it is about to trade into is not a
  strategy, it is a spoiler. The shock test below is the centre of this file: every bar's
  close is poisoned in turn, and no position any baseline took at or before that bar may
  move. A deliberately peeking rule is run through the same detector to prove the detector
  can actually see one.
* **Free trading.** A baseline that pays no friction beats everything and means nothing.
  Every entry and exit is charged by the statutory schedule in ``execution/friction.py``,
  and the tests check the charge lines by name, not just that some number was subtracted.
* **A ratio from a sample that cannot support one.** Sharpe travels with the t-statistic of
  the mean return behind it, and neither is printed below the repository's minimum count.
* **A confident expectancy from three trades.** Round trips and turnover are printed for
  every baseline, and below the minimum the expectancy is labelled rather than believed.

The price series are synthetic and generated here, because this environment has no market
data and no network to fetch any. Each one is built for a rule, and every rule is also run
on the series built for the others: a momentum baseline that makes money on a mean-reverting
series is not a momentum baseline, it is a fit.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from quant_ai.analytics.metrics import MINIMUM_RATIO_OBSERVATIONS
from quant_ai.backtesting.baselines import (
    MINIMUM_ROUND_TRIPS,
    BaselineEvaluator,
    BuyAndHoldBaseline,
    CashBaseline,
    LookaheadError,
    MeanReversionBaseline,
    PriceOnlyBaseline,
    TimeSeriesMomentumBaseline,
    VolatilityTargetBaseline,
    _assert_closed_history,
    baseline_instrument,
    daily_annualisation_periods,
    default_baselines,
    format_comparison,
)
from quant_ai.cli import main
from quant_ai.domain.models import AssetClass, Instrument, Market, OrderIntent, Side
from quant_ai.execution.friction import MarketFrictionModel
from quant_ai.execution.live_friction import friction_context_from_bars
from quant_ai.marketdata.models import Candle
from quant_ai.strategies.catalog import STRATEGY_CATALOG

IST = ZoneInfo("Asia/Kolkata")
FIRST_CLOSE = datetime(2025, 1, 1, 15, 30, tzinfo=IST)
INDIA = baseline_instrument(Market.INDIA)
BARS = 160

# Total one-way cost, in basis points of notional, of the flat ``backtest/costs.py`` model
# that used to sit beside this package: 1 commission + 2 slippage + 1 spread. It is written
# out here rather than imported because the class that held those three constants has been
# deleted, and this number now exists only as the thing the statutory note is measured
# against.
FLAT_BPS_STAND_IN = Decimal(4)


def _uniform(seed: int, count: int) -> list[float]:
    """A fixed pseudo-random sequence, written out rather than imported.

    A fixture that depends on the standard library's generator is a fixture that can change
    under a Python upgrade, and a behaviour test that moves for that reason teaches nothing.
    This linear congruential sequence is the same on every interpreter, forever.
    """
    state = seed
    values = []
    for _ in range(count):
        state = (1103515245 * state + 12345) % 2147483648
        values.append(state / 2147483648 * 2 - 1)
    return values


def series(closes: list[float], instrument: Instrument = INDIA) -> tuple[Candle, ...]:
    """Daily bars whose open is the previous close, so a gap never hides a cost."""
    bars = []
    for index, value in enumerate(closes):
        close = Decimal(str(round(value, 4)))
        opened = close if index == 0 else Decimal(str(round(closes[index - 1], 4)))
        high = (max(opened, close) * Decimal("1.006")).quantize(Decimal("0.0001"))
        low = (min(opened, close) * Decimal("0.994")).quantize(Decimal("0.0001"))
        bars.append(
            Candle(
                instrument,
                FIRST_CLOSE + timedelta(days=index),
                opened,
                high,
                low,
                close,
                Decimal(2000000),
            )
        )
    return tuple(bars)


def trending(count: int = BARS) -> tuple[Candle, ...]:
    """A drifting series with real down days, so a trend rule can still be wrong."""
    noise = _uniform(11, count)
    price, closes = 100.0, []
    for index in range(count):
        price *= 1 + 0.0045 + 0.010 * noise[index]
        closes.append(price)
    return series(closes)


def mean_reverting(count: int = BARS) -> tuple[Candle, ...]:
    """An Ornstein-Uhlenbeck-shaped series: no drift, and excursions that come back."""
    noise = _uniform(29, count)
    deviation, closes = 0.0, []
    for index in range(count):
        deviation = 0.80 * deviation + 0.030 * noise[index]
        closes.append(100.0 * (1 + deviation))
    return series(closes)


def flat(count: int = BARS) -> tuple[Candle, ...]:
    """One price, repeated. Any return here is a cost, and any cost here is a bug."""
    return series([100.0] * count)


def volatile(count: int = BARS) -> tuple[Candle, ...]:
    """Large swings around a level: what volatility targeting exists to shrink."""
    noise = _uniform(47, count)
    price, closes = 100.0, []
    for index in range(count):
        price *= 1 + 0.045 * noise[index]
        closes.append(price)
    return series(closes)


def shallow_chop(count: int = BARS) -> tuple[Candle, ...]:
    """Mean reversion whose amplitude is smaller than the round trip costs to harvest it.

    This is the series that makes the ordinary outcome visible: a rule that is right about
    the shape of the market and still loses money once the contract note is paid.
    """
    noise = _uniform(53, count)
    deviation, closes = 0.0, []
    for index in range(count):
        deviation = 0.55 * deviation + 0.0022 * noise[index]
        closes.append(100.0 * (1 + deviation))
    return series(closes)


def evaluator(**kwargs) -> BaselineEvaluator:
    return BaselineEvaluator(instrument=INDIA, **kwargs)


def report_for(reports, baseline_id):
    return next(item for item in reports if item.baseline_id == baseline_id)


def shock_close(bars: tuple[Candle, ...], index: int, factor: Decimal) -> tuple[Candle, ...]:
    """Replace one bar's close, leaving its open - the fill price - untouched."""
    bar = bars[index]
    close = (bar.close * factor).quantize(Decimal("0.0001"))
    return (
        bars[:index]
        + (
            Candle(
                bar.instrument,
                bar.timestamp,
                bar.open,
                max(bar.high, close, bar.open),
                min(bar.low, close, bar.open),
                close,
                bar.volume,
            ),
        )
        + bars[index + 1 :]
    )


class PeekingMomentum(PriceOnlyBaseline):
    """A rule that reads the close of the bar it is about to be filled into.

    It exists only to be caught. Without it the shock test below could pass because the
    shock is too weak, the series too smooth or the comparison too narrow, and nobody would
    know: a lookahead detector that has never detected anything is a comment, not a test.
    """

    baseline_id = "baseline.peeking"
    label = "Momentum that peeks at the same day's close"
    catalog_strategy_id = None
    warmup_bars = 2

    def __init__(self, whole_series: tuple[Candle, ...]) -> None:
        self.whole_series = whole_series

    def target_weight(self, history: tuple[Candle, ...], held_weight: Decimal) -> Decimal:
        del held_weight
        index = len(history)
        if index >= len(self.whole_series):
            return Decimal(0)
        return Decimal(1) if self.whole_series[index].close > history[-1].close else Decimal(0)


def positions_diverge(baseline_factory, bars: tuple[Candle, ...], indices) -> list[int]:
    """Indices at which poisoning one close moved a position taken at or before that bar.

    An empty list is the proof of no lookahead: the shocked close is invisible to every
    decision up to and including the bar it belongs to, which is only true if nothing read
    it. The same helper is pointed at a deliberate peeker to show it returns a non-empty
    list when there is something to find.
    """
    engine = evaluator()
    caught: list[int] = []
    for index in indices:
        for factor in (Decimal("1.5"), Decimal("0.6")):
            poisoned = shock_close(bars, index, factor)
            clean_run = engine.run(baseline_factory(bars), bars)
            poisoned_run = engine.run(baseline_factory(poisoned), poisoned)
            same_positions = clean_run.shares_held[: index + 1] == (
                poisoned_run.shares_held[: index + 1]
            )
            same_trades = [item for item in clean_run.trades if item.index <= index] == [
                item for item in poisoned_run.trades if item.index <= index
            ]
            if not (same_positions and same_trades):
                caught.append(index)
                break
    return caught


SHOCK_INDICES = tuple(range(2, BARS, 7))


@pytest.mark.parametrize("build", [trending, mean_reverting, volatile, shallow_chop])
def test_poisoning_a_bars_close_moves_no_position_taken_at_or_before_that_bar(build):
    """The single guard this whole deliverable rests on.

    A position for bar N is decided from bars up to N-1's close and filled at N's open, so
    N's own close cannot legally touch it. Here N's close is replaced with something 50%
    higher and then 40% lower, on four different market shapes, at every seventh bar. Every
    share held and every fill up to and including bar N must be byte-identical.

    This is what fails if the engine ever hands a baseline ``bars[:index + 1]``, if a rule
    starts reading the bar it trades into, or if the friction context is built from the
    execution bar instead of from the closed history.
    """
    bars = build()
    for baseline in default_baselines():
        caught = positions_diverge(lambda _bars, item=baseline: item, bars, SHOCK_INDICES)
        assert caught == [], f"{baseline.baseline_id} read a close it could not have seen"


def test_the_lookahead_detector_catches_a_rule_that_does_peek():
    """Proof that the test above can fail. Otherwise it proves nothing.

    ``PeekingMomentum`` does exactly what the guard forbids - it compares the execution
    bar's close against the last closed one - and the same sweep that clears all five
    baselines flags it at many bars.
    """
    bars = trending()
    caught = positions_diverge(PeekingMomentum, bars, SHOCK_INDICES)
    assert len(caught) > len(SHOCK_INDICES) // 2, caught


def test_the_engine_refuses_to_offer_a_baseline_the_bar_it_is_about_to_trade_into():
    """The engine-side half of the guard, independent of any particular rule.

    A rule cannot peek at what it is not given, so the slice handed to it is checked rather
    than trusted. Widening that slice by a single bar trips this, which is what makes the
    sabotage detectable even for a baseline whose signal happens not to use the close.
    """
    bars = trending(5)
    _assert_closed_history(bars[:2], bars[2])
    with pytest.raises(LookaheadError, match="lookahead_violation"):
        _assert_closed_history(bars[:3], bars[2])
    with pytest.raises(LookaheadError, match="lookahead_violation"):
        _assert_closed_history((), bars[0])


def test_every_fill_pays_the_statutory_contract_note_and_not_a_flat_bps_stand_in():
    """The costs charged here are the ones the ledger charges, line by line.

    The deleted ``backtest/costs.py`` model would charge 4 bps of notional and call it a
    day: no STT, no stamp duty, no depository charge on the delivery sell, no GST on the
    right base. A baseline priced that way trades almost free and beats anything. This
    reconstructs the exact friction the engine charged for a single buy-and-hold entry from
    :class:`MarketFrictionModel` itself, and asserts equality.
    """
    bars = trending()
    run = evaluator().run(BuyAndHoldBaseline(), bars)
    assert len(run.trades) == 1
    trade = run.trades[0]
    context = friction_context_from_bars(
        bars[: trade.index],
        liquidity_score=Decimal("0.90"),
        delivery=True,
        bars_per_session=1,
    )
    expected = MarketFrictionModel().evaluate(
        OrderIntent(
            INDIA.symbol,
            Market.INDIA,
            Side.BUY,
            trade.quantity,
            bars[trade.index].open,
            "baseline",
            AssetClass.EQUITY,
            "baseline",
        ),
        context,
    )
    assert trade.execution_price == expected.execution_price
    assert trade.cash_charges == expected.cash_charges
    codes = {item.code for item in expected.charges}
    assert {"BROKERAGE", "STT", "EXCHANGE", "SEBI", "GST", "STAMP"} <= codes
    # The one-way cost the deleted flat model would have charged on this same notional:
    # 1 bps commission + 2 bps slippage + 1 bps spread, and nothing else. The class is
    # gone; the number it produced is kept here as arithmetic so the comparison it existed
    # for outlives it. The real note is several times that, and the gap is the finding.
    flat_four_bps = (
        expected.execution_price * Decimal(trade.quantity) * FLAT_BPS_STAND_IN / Decimal(10000)
    )
    assert trade.cash_charges > flat_four_bps * Decimal(2)


def test_the_exit_leg_pays_the_depository_charge_a_buy_never_does():
    """Both legs are charged, and they are charged differently.

    A delivery sell debits the demat account, so it carries the DP fee and no stamp duty,
    while the buy carries stamp duty and no DP fee. A baseline that charged one schedule to
    both legs would understate the cost of every completed round trip.
    """
    run = evaluator().run(MeanReversionBaseline(), mean_reverting())
    assert run.round_trips, "the mean-reverting fixture must produce completed trades"
    sells = [item for item in run.trades if item.side == Side.SELL]
    buys = [item for item in run.trades if item.side == Side.BUY]
    assert sells and buys
    model = MarketFrictionModel()
    sample = sells[0]
    context = friction_context_from_bars(
        run_history(mean_reverting(), sample.index),
        liquidity_score=Decimal("0.90"),
        delivery=True,
        bars_per_session=1,
    )
    charges = {
        item.code: item.amount
        for item in model.evaluate(
            OrderIntent(
                INDIA.symbol, Market.INDIA, Side.SELL, sample.quantity,
                sample.reference_price, "baseline", AssetClass.EQUITY, "baseline",
            ),
            context,
        ).charges
    }
    assert charges["DP"] == Decimal("15.34")
    assert "STAMP" not in charges
    assert sample.cash_charges == sum(charges.values())


def run_history(bars: tuple[Candle, ...], index: int) -> tuple[Candle, ...]:
    return bars[:index]


def test_cash_is_the_floor_and_costs_exactly_nothing():
    """The one baseline that can only be tied, never beaten from below.

    Zero trades, zero friction, zero drawdown, a flat curve. If this one ever shows a
    return, the engine is charging or crediting something to a strategy that never placed
    an order.
    """
    for build in (trending, mean_reverting, flat, volatile):
        run = evaluator().run(CashBaseline(), build())
        assert run.trades == ()
        assert run.round_trips == ()
        assert run.cash_charges == Decimal(0)
        assert set(run.equity_curve) == {Decimal(100000)}
        report = report_for(evaluator().evaluate(build()), "baseline.cash")
        assert report.net_total_return == Decimal(0)
        assert report.max_drawdown == Decimal(0)
        assert report.turnover == Decimal(0)
        assert "Never traded" in " ".join(report.notes)
        # A curve with no dispersion has no risk-adjusted return. ``sharpe_ratio`` resolves
        # that 0/0 to zero; printing it would put the only unescorted ratio on the sheet.
        # The note has to say *which* guard withheld it - "no dispersion", not "too few
        # observations" - or the reader patches the wrong thing.
        assert report.sharpe is None and report.mean_return_t_statistic is None
        assert report.observations > MINIMUM_RATIO_OBSERVATIONS
        assert "no dispersion" in " ".join(report.notes)
        assert "below the" not in " ".join(report.notes)


def test_buy_and_hold_holds_once_and_pays_for_it_once():
    """One entry, no exit, and a curve that is the price curve minus that entry's friction.

    On a flat series the price contributes nothing, so everything buy-and-hold loses is the
    cost of getting in - which is the cheapest possible demonstration that friction is real
    here rather than nominal.
    """
    run = evaluator().run(BuyAndHoldBaseline(), flat())
    assert len(run.trades) == 1 and run.trades[0].side == Side.BUY
    assert run.open_position_at_end is True
    report = report_for(evaluator().evaluate(flat()), "baseline.buy_and_hold")
    assert report.gross_total_return == Decimal(0)
    assert report.net_total_return < Decimal(0)
    assert report.cost_drag > Decimal(0)
    assert report.round_trips == 0
    assert "still open at the last bar" in " ".join(report.notes)
    assert "expectancy and hit rate are undefined" in " ".join(report.notes)


def test_momentum_holds_a_trend_and_is_shredded_by_the_series_it_is_wrong_about():
    """The name has to mean something on one series and cost something on the other.

    On the trending fixture the rule stays invested and finishes near buy-and-hold. On the
    mean-reverting one it buys every bounce and sells every dip, so it round-trips many
    times more often and ends materially behind simply owning the thing. A momentum rule
    that made money on both would be curve-fitted to this file, not to momentum.
    """
    trend_reports = evaluator().evaluate(trending())
    trend = report_for(trend_reports, "baseline.momentum.v1")
    hold = report_for(trend_reports, "baseline.buy_and_hold")
    assert trend.net_total_return > Decimal(0)
    assert trend.round_trips <= 3

    chop_reports = evaluator().evaluate(mean_reverting())
    chop = report_for(chop_reports, "baseline.momentum.v1")
    chop_hold = report_for(chop_reports, "baseline.buy_and_hold")
    assert chop.round_trips > trend.round_trips
    assert chop.net_total_return < chop_hold.net_total_return
    assert chop.turnover > trend.turnover
    # And on the series it suits, it does not magically beat owning the asset for free.
    assert trend.net_total_return < trend.gross_total_return
    assert hold.net_total_return < hold.gross_total_return


def test_mean_reversion_trades_the_series_that_reverts_and_stands_aside_on_the_one_that_trends():
    """A 2-sigma dip only exists in a series that has a mean to fall below.

    On the mean-reverting fixture the rule completes real round trips. On the trending one
    the close is almost never two standard deviations under its own recent mean, so it
    barely trades: the right behaviour for a rule whose premise is absent, and the opposite
    of a rule that finds a signal in everything.
    """
    reverting = report_for(evaluator().evaluate(mean_reverting()), "baseline.mean_reversion")
    trend = report_for(evaluator().evaluate(trending()), "baseline.mean_reversion")
    assert reverting.round_trips >= 3
    assert reverting.round_trips > trend.round_trips
    assert reverting.gross_total_return > trend.gross_total_return
    assert trend.turnover < reverting.turnover


def test_volatility_targeting_shrinks_the_position_the_wilder_the_series_gets():
    """A risk control, measured as one: smaller exposure and a smaller hole, not more money.

    On the volatile fixture the weight is cut, so turnover and drawdown both fall well below
    buy-and-hold's. On the calm fixture there is nothing to cut and it is simply invested -
    stated here so nobody reads this baseline as a return forecast.
    """
    wild = evaluator().evaluate(volatile())
    targeted = report_for(wild, "baseline.volatility_target")
    hold = report_for(wild, "baseline.buy_and_hold")
    assert targeted.max_drawdown < hold.max_drawdown
    assert targeted.turnover < hold.turnover

    calm = evaluator().run(VolatilityTargetBaseline(), flat())
    assert calm.shares_held[-1] > 0, "with no volatility to target, it is fully invested"


def test_a_rule_that_is_right_about_the_market_and_still_loses_to_the_contract_note():
    """The ordinary outcome, and the one the operator most needs to see.

    ``shallow_chop`` reverts exactly the way the mean-reversion baseline expects, and the
    excursions are smaller than the friction needed to harvest them. Gross is positive, net
    is not, and the report says so in a field and in a sentence rather than leaving it to be
    inferred from two numbers in a table.
    """
    report = report_for(evaluator().evaluate(shallow_chop()), "baseline.mean_reversion")
    assert report.round_trips > 0
    assert report.gross_total_return > Decimal(0)
    assert report.net_total_return <= Decimal(0)
    assert report.beat_gross_and_lost_net is True
    assert "Profitable before costs" in " ".join(report.notes)
    assert report.turnover > Decimal(0)


def test_no_ratio_is_printed_from_a_sample_that_cannot_support_one():
    """The 6.67-Sharpe-with-a-0.16-t-statistic failure, in its cheapest form.

    A 25-bar window is below the repository's minimum, so the Sharpe, the Sortino and the
    t-statistic are all absent and the report says which minimum was missed. The same window
    at full length reports all three - the guard is a floor, not an off switch.
    """
    short = report_for(evaluator().evaluate(trending(25)), "baseline.buy_and_hold")
    assert short.observations < MINIMUM_RATIO_OBSERVATIONS
    assert short.sharpe is None and short.sortino is None
    assert short.mean_return_t_statistic is None
    assert f"below the {MINIMUM_RATIO_OBSERVATIONS}" in " ".join(short.notes)

    full = report_for(evaluator().evaluate(trending()), "baseline.buy_and_hold")
    assert full.sharpe is not None and full.mean_return_t_statistic is not None


def test_a_sharpe_never_appears_in_the_comparison_without_its_t_statistic():
    """The two numbers are one measurement, so the table may not separate them.

    A ratio annualised by sqrt(252) and a t-statistic scaled by sqrt(n) say the same thing
    at different scales; reading only the first is how noise gets promoted to a result.
    """
    reports = evaluator().evaluate(trending())
    rendered = format_comparison(reports)
    header = rendered.splitlines()[0]
    assert header.index("sharpe") < header.index("t_stat")
    for report in reports:
        row = next(line for line in rendered.splitlines() if line.startswith(report.baseline_id))
        if report.sharpe is None:
            continue
        assert f"{report.sharpe:.4f}" in row
        assert f"{report.mean_return_t_statistic:.4f}" in row
        assert report.to_dict()["meanReturnTStatistic"] is not None


def test_the_momentum_lookback_compares_the_last_close_to_the_one_n_bars_earlier():
    """The offset is the entire rule, so it is pinned rather than described.

    Off by one - comparing against ``history[-lookback]`` instead of ``history[-1 - lookback]``
    - is a 19-day momentum signal wearing a 20-day label, and every score computed from it
    would be attributed to the wrong hypothesis. Below the warmup the answer is flat, not
    a guess from a partial window.
    """
    baseline = TimeSeriesMomentumBaseline(lookback=3)
    bars = series([100.0, 90.0, 95.0, 99.0, 101.0])
    assert baseline.warmup_bars == 4
    assert baseline.target_weight(bars[:3], Decimal(0)) == Decimal(0)
    assert baseline.target_weight(bars[:4], Decimal(0)) == Decimal(0)
    assert baseline.target_weight(bars[:5], Decimal(0)) == Decimal(1)
    with pytest.raises(ValueError, match="lookback must be positive"):
        TimeSeriesMomentumBaseline(lookback=0)


def test_mean_reversion_enters_only_two_sigma_below_its_mean_and_leaves_at_the_mean():
    """Asymmetric by design, and measured in sigmas rather than in percent.

    A symmetric exit turns the rule into a trend follower and hides which of the two was
    being measured. A dispersion of zero yields no z-score at all, so a dead-flat window
    produces no position instead of dividing by nothing.
    """
    baseline = MeanReversionBaseline(lookback=10)
    dipped = series([100.0] * 9 + [95.0])
    recovered = series([95.0] + [100.0] * 9)
    calm = series([100.0, 101.0, 99.0] * 3 + [100.0])
    assert baseline.z_score(dipped) == Decimal(-3)
    assert baseline.target_weight(dipped, Decimal(0)) == Decimal(1)
    assert baseline.target_weight(dipped, Decimal(1)) == Decimal(1)
    assert baseline.target_weight(recovered, Decimal(1)) == Decimal(0)
    assert baseline.target_weight(calm, Decimal(0)) == Decimal(0)
    assert baseline.z_score(series([100.0] * 10)) is None


def test_a_report_cannot_be_constructed_with_a_sharpe_and_no_t_statistic():
    """The pairing is a constructor invariant, not a rendering convention.

    Suppressing the t-statistic while keeping the ratio is the exact edit that produced a
    Sharpe of 6.67 from a mean whose t-statistic was 0.16, so the report refuses to exist
    in that shape and no future renderer, exporter or dashboard can reintroduce it.
    """
    reports = evaluator().evaluate(trending())
    healthy = report_for(reports, "baseline.buy_and_hold")
    assert healthy.sharpe is not None
    with pytest.raises(ValueError, match="without the t-statistic"):
        dataclasses.replace(healthy, mean_return_t_statistic=None)


def test_the_expectancy_of_a_round_trip_is_what_the_account_kept():
    """Entry cost, exit cost and both drags are inside the number, not beside it.

    A round trip's P&L computed from two prices would flatter every baseline by the whole
    contract note. Here ``cash_out`` is everything the account paid to build the position
    and ``cash_in`` is everything it received on the way out, so the difference between the
    two is strictly worse than the price move that produced it.
    """
    bars = mean_reverting()
    run = evaluator().run(MeanReversionBaseline(), bars)
    assert run.round_trips
    for trip in run.round_trips:
        legs = [
            item
            for item in run.trades
            if trip.opened_index <= item.index <= trip.closed_index
        ]
        gross = sum(
            (item.notional if item.side == Side.SELL else -item.notional for item in legs),
            Decimal(0),
        )
        charges = sum((item.cash_charges for item in legs), Decimal(0))
        assert trip.net_pnl < gross
        assert abs((gross - trip.net_pnl) - charges) < Decimal("0.0000001")
    report = report_for(evaluator().evaluate(bars), "baseline.mean_reversion")
    assert report.expectancy_after_costs == sum(
        (item.net_pnl for item in run.round_trips), Decimal(0)
    ) / Decimal(len(run.round_trips))


def test_the_equity_curve_is_the_cash_the_trades_actually_left_behind():
    """The curve and the trade list have to be the same story, told twice.

    A round trip's P&L can be computed correctly while the account it was computed for was
    never debited - the expectancy then looks right and the equity curve is silently richer
    than the fills justify. This rebuilds the account from the trade list alone: every buy
    pays its notional and its charges, every sell receives its notional less its charges,
    and what is left plus the marked position must be the last point on the curve. The
    tolerance is a millionth of a rupee - the width of Decimal's 28-digit context after a
    few hundred compounding operations - which is far tighter than any missing charge.
    """
    for build in (trending, mean_reverting, volatile, shallow_chop):
        bars = build()
        for baseline in default_baselines():
            run = evaluator().run(baseline, bars)
            cash = Decimal(100000)
            for trade in run.trades:
                if trade.side == Side.BUY:
                    cash -= trade.notional + trade.cash_charges
                else:
                    cash += trade.notional - trade.cash_charges
            assert cash >= 0, f"{baseline.baseline_id} overdrew the account"
            rebuilt = cash + Decimal(run.shares_held[-1]) * bars[-1].close
            assert abs(run.equity_curve[-1] - rebuilt) < Decimal("0.000001")


def test_too_few_round_trips_is_reported_as_a_count_rather_than_as_confidence():
    """A three-trade expectancy is an anecdote with a decimal point.

    The count is always printed. Below the minimum the expectancy and hit rate are labelled
    descriptive, and the per-trade t-statistic that would turn them into evidence stays
    absent until the sample can carry one.
    """
    report = report_for(evaluator().evaluate(mean_reverting()), "baseline.mean_reversion")
    assert 0 < report.round_trips < MINIMUM_ROUND_TRIPS
    assert report.expectancy_after_costs is not None
    assert report.hit_rate is not None
    assert report.trade_t_statistic is None
    assert f"{report.round_trips} round trips, below {MINIMUM_ROUND_TRIPS}" in " ".join(
        report.notes
    )
    assert "not evidence of an edge" in " ".join(report.notes)


def test_daily_bars_annualise_against_sessions_and_an_intraday_series_is_refused():
    """The scale a ratio is quoted in, taken from the calendar rather than assumed.

    ``annualisation_periods`` exists so a one-minute Sharpe is not a daily one. The daily
    basis is one sample per session, and a series sampled faster than that is refused
    outright: silently annualising minute bars by 252 understates the ratio, and silently
    annualising them by 98280 overstates it, and the evaluator is not entitled to guess.
    """
    assert daily_annualisation_periods(Market.INDIA, "NSE") == 252
    assert daily_annualisation_periods(Market.USA) == 252
    report = report_for(evaluator().evaluate(trending()), "baseline.buy_and_hold")
    assert report.periods_per_year == 252

    minutes = tuple(
        Candle(INDIA, FIRST_CLOSE + timedelta(minutes=index), *[Decimal(100)] * 4, Decimal(1000))
        for index in range(40)
    )
    with pytest.raises(ValueError, match="expects daily bars"):
        evaluator().evaluate(minutes)


def test_a_diwali_muhurat_session_is_a_daily_bar_and_is_not_refused():
    """The real NSE calendar, which the old spacing rule rejected outright.

    On 24 October 2022 NSE closed its regular session for Diwali and held only the one-hour
    Muhurat sitting at 18:15 IST. The next session opened at 09:15 the following morning -
    fifteen hours later, and the only pair under twenty hours in a real 19-year INFY series
    of 4,660 bars. That is one bar per session, so it is a daily series, and requiring a
    minimum wall-clock spacing refused every Indian series spanning a Diwali.
    """
    bars = list(trending()[:40])
    # This fixture stamps each bar at 15:30 IST. Pull one bar back to 09:15 on its own
    # date and the gap from the previous session closes to under twenty hours, exactly as
    # the morning after a Muhurat sitting does - while every bar keeps its own date.
    bars[20] = replace(bars[20], timestamp=bars[20].timestamp - timedelta(hours=6, minutes=15))
    assert bars[20].timestamp - bars[19].timestamp < timedelta(hours=20)
    assert len({bar.timestamp.astimezone(IST).date() for bar in bars}) == len(bars)

    reports = evaluator().evaluate(tuple(bars))

    assert [report.bars for report in reports] == [len(bars)] * len(reports)


def test_the_date_that_counts_is_the_venues_and_not_utc():
    """Two sessions can share a UTC date and still be two sessions.

    India runs five and a half hours ahead, so an evening bar and the next morning's sit on
    one UTC date while belonging to two Indian ones - and on MCX, whose session runs to
    23:30 IST, that pairing is ordinary rather than exotic. Counting UTC dates would refuse
    a perfectly good daily series as intraday, so the rule reads the venue's day.

    Written because the first version of these tests did not catch it: this fixture stamps
    bars at 15:30 IST, which is 10:00 UTC on the same date, so UTC and IST agree and a
    UTC-based rule passes every one of them.
    """
    bars = [replace(bar, timestamp=bar.timestamp.astimezone(timezone.utc)) for bar in
            trending()[:40]]
    # Stamped in UTC, as a fetched dataset is: reading the date off the stamp is then the
    # UTC day, and only converting to the venue's zone gives the trading day.
    evening = bars[19].timestamp.astimezone(IST).replace(hour=23, minute=0)
    morning = bars[20].timestamp.astimezone(IST).replace(hour=4, minute=0)
    bars[19] = replace(bars[19], timestamp=evening.astimezone(timezone.utc))
    bars[20] = replace(bars[20], timestamp=morning.astimezone(timezone.utc))

    assert bars[19].timestamp.date() == bars[20].timestamp.date()          # one UTC day
    assert evening.astimezone(IST).date() != morning.astimezone(IST).date()  # two IST days

    reports = evaluator().evaluate(tuple(bars))
    assert [report.bars for report in reports] == [len(bars)] * len(reports)


def test_two_bars_on_one_trading_date_are_still_refused_however_far_apart():
    """What the rule is actually for, stated as the thing it means.

    ``daily_annualisation_periods`` assumes one sample per session. A date carrying two
    bars is an intraday series whatever the clock says between them, and annualising it
    against 252 is the inflated Sharpe this module exists to stop printing.
    """
    bars = list(trending()[:40])
    # Two bars on one date, twenty-three hours apart: a spacing rule of any plausible size
    # waves this through, and it is unambiguously an intraday pair.
    day = bars[19].timestamp.astimezone(IST).date()
    opened = datetime.combine(day, time(0, 30), tzinfo=IST)
    bars[19] = replace(bars[19], timestamp=opened)
    bars[20] = replace(bars[20], timestamp=opened + timedelta(hours=23))
    assert bars[20].timestamp - bars[19].timestamp > timedelta(hours=20)

    with pytest.raises(ValueError, match="carries two"):
        evaluator().evaluate(tuple(bars))


def test_a_misordered_series_is_refused_rather_than_silently_reordered():
    """Ordering used to fall out of the spacing comparison; now it is checked on its own.

    A negative gap was smaller than the minimum, so a series out of order tripped the old
    rule by accident. Counting bars per date would not notice, so the check is explicit.
    """
    bars = list(trending()[:40])
    bars[20], bars[21] = bars[21], bars[20]

    with pytest.raises(ValueError, match="strictly increasing"):
        evaluator().evaluate(tuple(bars))


def test_the_curve_carries_exactly_one_mark_per_bar_and_one_return_per_step():
    """The denominator of every ratio on the sheet, checked rather than assumed.

    The deleted ``backtest/replay.py`` engine asserted its own version of this as
    ``len(equity_curve) == len(bars) + 1``, because it marked the account once before the
    first bar and again after every bar including it. This engine marks once at the
    starting capital and once per execution bar, so the curve is exactly as long as the
    series and the return series is one shorter.

    The count is not cosmetic. ``observations`` is the *n* under the square root in the
    annualisation and the *n* in the t-statistic's standard error, so a curve that marked a
    bar twice, or skipped one, would move every Sharpe and every t-statistic printed beside
    it without moving a single trade.
    """
    for build in (trending, mean_reverting, volatile, shallow_chop, flat):
        bars = build()
        for baseline in default_baselines():
            run = evaluator().run(baseline, bars)
            assert len(run.equity_curve) == len(bars), baseline.baseline_id
            assert len(run.shares_held) == len(bars), baseline.baseline_id
            assert len(run.returns) == len(bars) - 1, baseline.baseline_id
        for report in evaluator().evaluate(bars):
            assert report.bars == len(bars)
            assert report.observations == len(bars) - 1
            assert report.mean_return_observations in (0, len(bars) - 1)


def test_a_baseline_is_a_pure_function_of_closed_bars_and_its_own_holding():
    """Run it twice, get the same trades. That is the whole claim of "deterministic".

    A baseline that accumulated state between runs would make the comparison against the
    swarm unrepeatable, and an unrepeatable baseline is not a baseline.
    """
    bars = mean_reverting()
    for baseline in default_baselines():
        first = evaluator().run(baseline, bars)
        second = evaluator().run(baseline, bars)
        assert first.trades == second.trades
        assert first.equity_curve == second.equity_curve
        assert first.round_trips == second.round_trips


def test_a_baseline_that_asks_for_leverage_or_a_short_is_refused():
    """Long-only and unlevered, because that is what the pilot can actually place.

    A baseline quietly permitted to go to 150% or to -100% would not be a floor under the
    swarm; it would be a different strategy with a different risk, scored as if it were the
    same one.
    """

    class Greedy(PriceOnlyBaseline):
        baseline_id = "baseline.greedy"
        label = "levered"
        warmup_bars = 1

        def __init__(self, weight: Decimal) -> None:
            self.weight = weight

        def target_weight(self, history, held_weight):
            del history, held_weight
            return self.weight

    for weight in (Decimal("1.5"), Decimal("-0.5")):
        with pytest.raises(ValueError, match="long-only and unlevered"):
            evaluator().run(Greedy(weight), trending())


def test_the_baselines_name_the_catalogued_hypotheses_they_are_the_floor_for():
    """A floor nobody can match to a hypothesis is a floor nobody will use.

    ``baseline.momentum.v1`` and ``baseline.mean_reversion`` carry the catalogue ids the
    dashboard and the Python catalogue already declare, so a reader comparing the swarm's
    momentum run against a number knows it is the same hypothesis. Buy-and-hold, cash and
    volatility targeting are deliberately not hypotheses and say so with ``None``.
    """
    python_ids = {item["id"] for item in STRATEGY_CATALOG}
    typescript = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "apps/pramana-ui/lib/strategy-catalog.ts"
    ).read_text()
    declared = {
        item.catalog_strategy_id
        for item in default_baselines()
        if item.catalog_strategy_id is not None
    }
    assert declared == {"momentum.v1", "mean_reversion"}
    for identifier in declared:
        assert identifier in python_ids
        assert f'id:"{identifier}"' in typescript
    assert {item.baseline_id for item in default_baselines()} == {
        "baseline.cash",
        "baseline.buy_and_hold",
        "baseline.momentum.v1",
        "baseline.mean_reversion",
        "baseline.volatility_target",
    }


def test_the_operator_command_scores_every_baseline_and_writes_the_evidence(tmp_path, monkeypatch, capsys):
    """What an operator runs once there is a real dataset, end to end.

    ``pramana baselines --data <file> --market india`` reads the same replay JSON the
    backtest command reads, registers the five evaluations against the trial register so a
    sweep cannot later be reported as one lucky look, prints the comparison and leaves a
    machine-readable copy next to the proofs.
    """
    monkeypatch.setenv("QUANT_AI_PAPER_DB", str(tmp_path / "ledger.db"))
    monkeypatch.setenv("PRAMANA_XAI_DIR", str(tmp_path / "xai"))
    dataset = tmp_path / "daily.json"
    dataset.write_text(
        json.dumps(
            {
                "bars": [
                    {
                        "timestamp": bar.timestamp.isoformat(),
                        "open": str(bar.open),
                        "high": str(bar.high),
                        "low": str(bar.low),
                        "close": str(bar.close),
                        "volume": str(bar.volume),
                    }
                    for bar in trending()
                ]
            }
        )
    )
    assert main(["baselines", "--data", str(dataset), "--market", "india"]) == 0
    printed = capsys.readouterr().out
    payload = json.loads(printed.splitlines()[-1])
    assert payload["schema"] == "pramana.baseline_comparison.v1"
    assert [item["baselineId"] for item in payload["reports"]] == [
        item.baseline_id for item in default_baselines()
    ]
    assert all(item["annualisationPeriodsPerYear"] == 252 for item in payload["reports"])
    assert "baseline.cash" in printed and "t_stat" in printed
    written = json.loads((tmp_path / "xai" / "latest-baselines.json").read_text())
    assert written == payload


def test_the_baselines_command_refuses_a_run_with_no_data():
    with pytest.raises(SystemExit, match="baselines requires --data"):
        main(["baselines"])
