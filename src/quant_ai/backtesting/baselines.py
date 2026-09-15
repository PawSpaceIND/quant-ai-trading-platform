"""Price-only baselines and the evaluator that scores them, so the swarm has a number to beat.

Nothing in this platform had ever been measured against anything. "The AI decided X" is
not evidence until there is a dumber, cheaper, fully deterministic rule that decided the
same thing for free, and lost. This module is that rule set, and the scorer for it.

Three properties make the comparison honest, and each is enforced rather than promised:

* **Price only.** A baseline sees closed daily bars and its own current holding. No news,
  no fundamentals, no macro, no model. Every one of them is a pure function of those two
  inputs, so two runs over the same bars produce the same trades forever.
* **Real friction.** Every entry and exit is priced by :class:`MarketFrictionModel`, the
  same statutory schedule the paper ledger charges - STT, exchange, SEBI, stamp duty, the
  depository charge on a delivery sell, GST at 18% on the service base only - plus the
  spread and square-root impact the ledger applies. A baseline that beats the swarm only
  because it trades free would be worse than no baseline at all, so the gross curve is
  computed too and reported beside the net one: the gap between them is the whole point.
* **No lookahead.** The position held through bar ``N`` is decided from bars strictly
  before ``N``, and filled at ``N``'s open. The engine refuses to hand a baseline a bar at
  or after the bar it is about to trade into, and raises :class:`LookaheadError` if asked.

Reporting rules that this module will not let a caller break:

* A Sharpe never travels without the t-statistic of the mean return that produced it.
  A ratio of 6.67 and a t-statistic of 0.16 are the same measurement at two scales; seeing
  only the first is how a coin flip gets promoted.
* Ratios are annualised against :func:`annualisation_periods` at the sampling interval of
  the series. Daily bars annualise with 252, not with the session's minute count, and the
  evaluator refuses a series whose bars are not daily rather than scaling one to the other.
* Below :data:`MINIMUM_RATIO_OBSERVATIONS` observations no ratio is printed at all, and
  below :data:`MINIMUM_ROUND_TRIPS` closed trades the expectancy and hit rate are marked as
  descriptive rather than evidence. Turnover and the round-trip count are always printed,
  because "beat buy-and-hold gross, lost after costs" is the ordinary outcome and it is
  only visible if those two numbers are.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal
from math import sqrt
from statistics import mean, pstdev

from quant_ai.analytics.metrics import (
    MINIMUM_RATIO_OBSERVATIONS,
    MINIMUM_SIGNIFICANCE_OBSERVATIONS,
    MeanReturnSignificance,
    PerformanceMetrics,
    annualisation_periods,
    mean_return_significance,
    summarize_performance,
)
from quant_ai.domain.models import AssetClass, Instrument, Market, OrderIntent, Side
from quant_ai.execution.friction import FrictionContext, MarketFrictionModel
from quant_ai.execution.live_friction import friction_context_from_bars
from quant_ai.execution.session import regular_session_length
from quant_ai.marketdata.models import Candle

# A mean of a handful of trades has a standard error the size of itself. Below this count
# the expectancy and hit rate are still printed - suppressing them would hide that the
# strategy barely traded - but they are labelled as descriptive, and the t-statistic that
# would make them evidence stays absent until the sample can support one.
MINIMUM_ROUND_TRIPS = 20

# One daily bar per session. Anything sampled faster is not a daily series, and a daily
# annualisation applied to it is the error this module exists to stop printing. Weekends
# and holidays make gaps longer, never shorter, so only the lower bound is checked.
MINIMUM_DAILY_BAR_SPACING = timedelta(hours=20)

# What replay scores a historical counter at. The baseline uses the same figure so a
# baseline fill and a replayed fill cannot be priced by two different assumptions.
REPLAY_LIQUIDITY_SCORE = Decimal("0.90")

SIGNIFICANCE_NOTE = (
    "The t-statistic tests the mean per-bar return against zero on the observations shown. "
    "It is not corrected for multiple testing, and per-bar returns are serially correlated, "
    "so it is an upper bound on how much the Sharpe beside it is worth."
)


class LookaheadError(RuntimeError):
    """A baseline was offered a bar it could not have seen when the position was decided."""


def daily_annualisation_periods(
    market: Market = Market.INDIA, exchange: str | None = None
) -> int:
    """Sampling intervals in a trading year for a one-bar-per-session series.

    Derived from :func:`annualisation_periods` rather than hardcoded to 252, and derived
    with the sampling interval set equal to the session length, because that is what a
    daily bar is: one sample per session. Handing this function a one-minute interval by
    mistake is impossible - it takes no interval - and handing the *evaluator* one-minute
    bars raises instead of quietly annualising them against the wrong basis.
    """
    session = regular_session_length(market, exchange)
    return annualisation_periods(session, session)


@dataclass(frozen=True)
class BaselineTrade:
    """One executed leg, at the price and cost the friction model charged for it."""

    index: int
    side: Side
    quantity: int
    reference_price: Decimal
    execution_price: Decimal
    cash_charges: Decimal
    spread_drag: Decimal
    slippage_drag: Decimal

    @property
    def notional(self) -> Decimal:
        return self.execution_price * Decimal(self.quantity)


@dataclass(frozen=True)
class RoundTrip:
    """A flat-to-flat cycle: every rupee that went out, and every rupee that came back.

    ``net_pnl`` is after friction on both legs, including on any partial trims in between,
    so it is what the account actually kept rather than the difference between two prices.
    """

    opened_index: int
    closed_index: int
    cash_out: Decimal
    cash_in: Decimal

    @property
    def net_pnl(self) -> Decimal:
        return self.cash_in - self.cash_out

    @property
    def net_return(self) -> Decimal:
        return self.net_pnl / self.cash_out if self.cash_out > 0 else Decimal(0)


class PriceOnlyBaseline(ABC):
    """A rule that maps closed bars and the current holding to a long-only target weight.

    Subclasses hold configuration and no run state. The holding is passed in rather than
    remembered so a baseline cannot accumulate hidden history between runs, which is what
    would make two runs over the same bars disagree.
    """

    baseline_id: str
    label: str
    # The catalogued hypothesis this baseline is the deterministic floor for, or None when
    # it is not a hypothesis at all (holding the asset, or holding nothing).
    catalog_strategy_id: str | None = None
    # Closed bars needed before the rule can say anything. Below it the target is flat.
    warmup_bars: int = 1

    @abstractmethod
    def target_weight(self, history: tuple[Candle, ...], held_weight: Decimal) -> Decimal:
        """Fraction of equity to hold through the next bar, in [0, 1].

        ``history`` ends strictly before the bar this weight will be filled into. Reading
        anything outside it is the lookahead the engine and its tests exist to catch.
        """
        raise NotImplementedError


class BuyAndHoldBaseline(PriceOnlyBaseline):
    """Own the thing. The number any active strategy has to justify its turnover against."""

    baseline_id = "baseline.buy_and_hold"
    label = "Buy and hold"
    catalog_strategy_id = None
    warmup_bars = 1

    def target_weight(self, history: tuple[Candle, ...], held_weight: Decimal) -> Decimal:
        del history, held_weight
        return Decimal(1)


class CashBaseline(PriceOnlyBaseline):
    """Hold nothing. The floor: zero return, zero cost, zero drawdown, zero excuses.

    A strategy that cannot beat this one has not earned the right to trade, and the only
    way to score below it is to pay friction for nothing.
    """

    baseline_id = "baseline.cash"
    label = "Cash (always flat)"
    catalog_strategy_id = None
    warmup_bars = 0

    def target_weight(self, history: tuple[Candle, ...], held_weight: Decimal) -> Decimal:
        del history, held_weight
        return Decimal(0)


class TimeSeriesMomentumBaseline(PriceOnlyBaseline):
    """Long while the last close is above the close ``lookback`` bars earlier, else flat.

    The whole rule. It is the deterministic floor under the ``momentum.v1`` hypothesis: if
    a swarm cannot beat one comparison of two closes, it is not finding momentum, it is
    paying for it.
    """

    baseline_id = "baseline.momentum.v1"
    label = "Time-series momentum"
    catalog_strategy_id = "momentum.v1"

    def __init__(self, lookback: int = 20) -> None:
        if lookback < 1:
            raise ValueError("momentum lookback must be positive")
        self.lookback = lookback
        self.warmup_bars = lookback + 1

    def target_weight(self, history: tuple[Candle, ...], held_weight: Decimal) -> Decimal:
        del held_weight
        if len(history) <= self.lookback:
            return Decimal(0)
        return (
            Decimal(1)
            if history[-1].close > history[-1 - self.lookback].close
            else Decimal(0)
        )


class MeanReversionBaseline(PriceOnlyBaseline):
    """Buy a close ``entry_sigma`` standard deviations below its own mean; exit at the mean.

    The dispersion is the population standard deviation of the closes in the window, the
    same estimator :func:`sharpe_ratio` uses, so the sigma here and the sigma in the score
    mean the same thing. Entry and exit are deliberately asymmetric: waiting for a
    symmetric +2 sigma exit turns a mean-reversion rule into a trend rule and hides which
    of the two is being measured.
    """

    baseline_id = "baseline.mean_reversion"
    label = "Mean reversion after a 2-sigma move"
    catalog_strategy_id = "mean_reversion"

    def __init__(
        self,
        lookback: int = 20,
        entry_sigma: Decimal = Decimal(2),
        exit_sigma: Decimal = Decimal(0),
    ) -> None:
        if lookback < 2:
            raise ValueError("mean-reversion lookback must be at least 2")
        if entry_sigma <= 0:
            raise ValueError("entry_sigma must be positive")
        if exit_sigma < -entry_sigma:
            raise ValueError("exit_sigma must sit above the entry threshold")
        self.lookback = lookback
        self.entry_sigma = entry_sigma
        self.exit_sigma = exit_sigma
        self.warmup_bars = lookback

    def z_score(self, history: tuple[Candle, ...]) -> Decimal | None:
        if len(history) < self.lookback:
            return None
        closes = [float(bar.close) for bar in history[-self.lookback :]]
        dispersion = pstdev(closes)
        if dispersion <= 0:
            return None
        return Decimal(str((closes[-1] - mean(closes)) / dispersion))

    def target_weight(self, history: tuple[Candle, ...], held_weight: Decimal) -> Decimal:
        z = self.z_score(history)
        if z is None:
            return Decimal(0)
        if held_weight > 0:
            return Decimal(0) if z >= self.exit_sigma else Decimal(1)
        return Decimal(1) if z <= -self.entry_sigma else Decimal(0)


class VolatilityTargetBaseline(PriceOnlyBaseline):
    """Hold the weight whose realised volatility matches ``target_volatility``, capped at 1.

    This is a risk control, not a return forecast: on a calm series it is fully invested,
    and it will happily ride a quiet market down. What it is a baseline *for* is the claim
    that a strategy's Sharpe came from choosing when to be exposed rather than from simply
    being smaller when the market was wild - a claim that costs one standard deviation to
    test and is worth testing before attributing anything to a model.

    ``rebalance_band`` exists because a weight recomputed every bar would rebalance every
    bar, and the resulting turnover would be a property of the arithmetic rather than of
    the market. It is reported: see ``turnover`` on the report.
    """

    baseline_id = "baseline.volatility_target"
    label = "Volatility targeting"
    catalog_strategy_id = None

    def __init__(
        self,
        lookback: int = 20,
        target_volatility: Decimal = Decimal("0.15"),
        max_weight: Decimal = Decimal(1),
        rebalance_band: Decimal = Decimal("0.10"),
        periods_per_year: int | None = None,
    ) -> None:
        if lookback < 2:
            raise ValueError("volatility lookback must be at least 2")
        if target_volatility <= 0:
            raise ValueError("target_volatility must be positive")
        if not Decimal(0) < max_weight <= Decimal(1):
            raise ValueError("max_weight must be in (0,1]: this baseline is long-only, unlevered")
        if rebalance_band < 0:
            raise ValueError("rebalance_band cannot be negative")
        self.lookback = lookback
        self.target_volatility = target_volatility
        self.max_weight = max_weight
        self.rebalance_band = rebalance_band
        self.periods_per_year = periods_per_year or daily_annualisation_periods()
        self.warmup_bars = lookback + 1

    def realised_volatility(self, history: tuple[Candle, ...]) -> Decimal | None:
        """Annualised population standard deviation of the closing returns in the window."""
        if len(history) <= self.lookback:
            return None
        closes = [bar.close for bar in history[-self.lookback - 1 :]]
        returns = [
            float((after - before) / before)
            for before, after in zip(closes, closes[1:])
            if before > 0
        ]
        if len(returns) < 2:
            return None
        return Decimal(str(pstdev(returns) * sqrt(self.periods_per_year)))

    def target_weight(self, history: tuple[Candle, ...], held_weight: Decimal) -> Decimal:
        volatility = self.realised_volatility(history)
        if volatility is None:
            return held_weight
        target = (
            self.max_weight
            if volatility <= 0
            else min(self.max_weight, self.target_volatility / volatility)
        )
        if abs(target - held_weight) < self.rebalance_band:
            return held_weight
        return target


def default_baselines() -> tuple[PriceOnlyBaseline, ...]:
    """The five floors, cheapest first. ``baseline.cash`` is the one nothing may score under."""
    return (
        CashBaseline(),
        BuyAndHoldBaseline(),
        TimeSeriesMomentumBaseline(),
        MeanReversionBaseline(),
        VolatilityTargetBaseline(),
    )


@dataclass(frozen=True)
class BaselineRun:
    """The raw path: what was held, what it cost, and what the account was worth."""

    equity_curve: tuple[Decimal, ...]
    returns: tuple[Decimal, ...]
    shares_held: tuple[int, ...]
    trades: tuple[BaselineTrade, ...]
    round_trips: tuple[RoundTrip, ...]
    open_position_at_end: bool

    @property
    def spread_drag(self) -> Decimal:
        return sum((item.spread_drag for item in self.trades), Decimal(0))

    @property
    def slippage_drag(self) -> Decimal:
        return sum((item.slippage_drag for item in self.trades), Decimal(0))

    @property
    def cash_charges(self) -> Decimal:
        return sum((item.cash_charges for item in self.trades), Decimal(0))

    @property
    def traded_notional(self) -> Decimal:
        return sum((item.notional for item in self.trades), Decimal(0))


@dataclass(frozen=True)
class BaselineReport:
    """One baseline's score. Every ratio here is escorted by what the sample can support."""

    baseline_id: str
    label: str
    catalog_strategy_id: str | None
    bars: int
    observations: int
    periods_per_year: int
    starting_capital: Decimal
    final_equity: Decimal
    net_total_return: Decimal
    gross_total_return: Decimal
    cost_drag: Decimal
    expectancy_after_costs: Decimal | None
    expectancy_return: Decimal | None
    round_trips: int
    hit_rate: Decimal | None
    turnover: Decimal
    trades: int
    open_position_at_end: bool
    sharpe: Decimal | None
    sortino: Decimal | None
    max_drawdown: Decimal
    mean_return_t_statistic: Decimal | None
    mean_return_standard_error: Decimal | None
    mean_return_observations: int
    trade_t_statistic: Decimal | None
    spread_drag: Decimal
    slippage_drag: Decimal
    cash_charges: Decimal
    notes: tuple[str, ...]
    minimum_ratio_observations: int = MINIMUM_RATIO_OBSERVATIONS
    minimum_round_trips: int = MINIMUM_ROUND_TRIPS
    significance_note: str = SIGNIFICANCE_NOTE

    def __post_init__(self) -> None:
        """A Sharpe may not exist on a report whose mean return has no t-statistic.

        The two are one measurement at two scales - sqrt(periods) against sqrt(n) - and a
        report that carries the flattering one alone is the exact shape of the 6.67-Sharpe,
        0.16-t-statistic claim this module was written to stop. Enforced in the constructor
        rather than in the renderer, so no future caller can format its way around it.
        """
        if self.sharpe is not None and self.mean_return_t_statistic is None:
            raise ValueError(
                f"{self.baseline_id}: a Sharpe cannot be reported without the t-statistic "
                "of the mean return behind it"
            )

    @property
    def total_friction(self) -> Decimal:
        return self.spread_drag + self.slippage_drag + self.cash_charges

    @property
    def beat_gross_and_lost_net(self) -> bool:
        """The ordinary outcome, named so a reader cannot miss it in a column of numbers."""
        return self.gross_total_return > 0 >= self.net_total_return

    def to_dict(self) -> dict:
        payload = {
            "baselineId": self.baseline_id,
            "label": self.label,
            "catalogStrategyId": self.catalog_strategy_id,
            "bars": self.bars,
            "observations": self.observations,
            "annualisationPeriodsPerYear": self.periods_per_year,
            "startingCapital": str(self.starting_capital),
            "finalEquity": str(self.final_equity),
            "netTotalReturn": str(self.net_total_return),
            "grossTotalReturn": str(self.gross_total_return),
            "costDrag": str(self.cost_drag),
            "expectancyAfterCosts": _optional(self.expectancy_after_costs),
            "expectancyReturn": _optional(self.expectancy_return),
            "roundTrips": self.round_trips,
            "hitRate": _optional(self.hit_rate),
            "turnover": str(self.turnover),
            "trades": self.trades,
            "openPositionAtEnd": self.open_position_at_end,
            "sharpe": _optional(self.sharpe),
            "sortino": _optional(self.sortino),
            "maxDrawdown": str(self.max_drawdown),
            "meanReturnTStatistic": _optional(self.mean_return_t_statistic),
            "meanReturnStandardError": _optional(self.mean_return_standard_error),
            "meanReturnObservations": self.mean_return_observations,
            "tradeTStatistic": _optional(self.trade_t_statistic),
            "spreadDrag": str(self.spread_drag),
            "slippageDrag": str(self.slippage_drag),
            "cashCharges": str(self.cash_charges),
            "totalFriction": str(self.total_friction),
            "beatGrossAndLostNet": self.beat_gross_and_lost_net,
            "minimumRatioObservations": self.minimum_ratio_observations,
            "minimumRoundTrips": self.minimum_round_trips,
            "notes": list(self.notes),
            "significanceNote": self.significance_note,
        }
        return payload


def _optional(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


class BaselineEvaluator:
    """Runs every baseline over one dataset and scores it against the real cost model.

    The same bars, the same friction model and the same sizing rule are given to each
    baseline, so the only thing that differs between two reports is the rule itself. The
    gross curve is the identical run with a zero-cost model in place of the real one; the
    difference between the two is friction and nothing else.
    """

    def __init__(
        self,
        *,
        instrument: Instrument | None = None,
        starting_capital: Decimal = Decimal(100000),
        friction_model: MarketFrictionModel | None = None,
        liquidity_score: Decimal = REPLAY_LIQUIDITY_SCORE,
        delivery: bool = True,
        observed_half_spread_fraction: Decimal | None = None,
        tenant_id: str = "baseline",
    ) -> None:
        if starting_capital <= 0:
            raise ValueError("starting_capital must be positive")
        self.instrument = instrument
        self.starting_capital = starting_capital
        self.friction_model = friction_model or MarketFrictionModel()
        # The one model that must be free, and the only place a zero-cost fill is allowed:
        # it exists to measure the real one, never to stand in for it.
        self.gross_friction_model = MarketFrictionModel.compatibility(Decimal(0))
        self.liquidity_score = liquidity_score
        self.delivery = delivery
        self.observed_half_spread_fraction = observed_half_spread_fraction
        self.tenant_id = tenant_id

    def evaluate(
        self,
        bars: tuple[Candle, ...],
        baselines: tuple[PriceOnlyBaseline, ...] | None = None,
    ) -> tuple[BaselineReport, ...]:
        bars = tuple(bars)
        _assert_daily_bars(bars)
        instrument = self.instrument or bars[0].instrument
        periods = daily_annualisation_periods(instrument.market, instrument.exchange)
        return tuple(
            self._report(baseline, bars, instrument, periods)
            for baseline in (baselines if baselines is not None else default_baselines())
        )

    def run(
        self,
        baseline: PriceOnlyBaseline,
        bars: tuple[Candle, ...],
        *,
        instrument: Instrument | None = None,
        friction_model: MarketFrictionModel | None = None,
    ) -> BaselineRun:
        """One baseline over one series. Exposed so a test can inspect the path itself."""
        bars = tuple(bars)
        _assert_daily_bars(bars)
        return self._run(
            baseline,
            bars,
            instrument or self.instrument or bars[0].instrument,
            friction_model or self.friction_model,
        )

    def _report(
        self,
        baseline: PriceOnlyBaseline,
        bars: tuple[Candle, ...],
        instrument: Instrument,
        periods: int,
    ) -> BaselineReport:
        """Score one baseline twice: once paying friction, once not.

        The gross run takes its own path rather than being reconstructed by adding the
        costs back, because a cheaper account compounds differently and sizes differently.
        Every difference between the two curves is therefore friction and only friction.
        """
        net = self._run(baseline, bars, instrument, self.friction_model)
        gross = self._run(baseline, bars, instrument, self.gross_friction_model)
        trade_pnls = tuple(item.net_pnl for item in net.round_trips)
        trade_returns = tuple(item.net_return for item in net.round_trips)
        metrics = summarize_performance(
            net.returns,
            net.equity_curve,
            trade_pnls,
            periods=periods,
        )
        significance = mean_return_significance(net.returns)
        trade_significance = mean_return_significance(trade_returns)
        # A curve with no dispersion has no risk-adjusted return: it has no risk, and the
        # 0/0 that :func:`sharpe_ratio` resolves to zero would be the only ratio on the
        # sheet with no t-statistic beside it. Withhold both together instead.
        if significance is None:
            metrics = replace(metrics, sharpe=None, sortino=None)
        net_total = _total_return(net.equity_curve, self.starting_capital)
        gross_total = _total_return(gross.equity_curve, self.starting_capital)
        average_equity = (
            sum(net.equity_curve, Decimal(0)) / Decimal(len(net.equity_curve))
            if net.equity_curve
            else self.starting_capital
        )
        wins = sum(1 for item in trade_pnls if item > 0)
        return BaselineReport(
            baseline_id=baseline.baseline_id,
            label=baseline.label,
            catalog_strategy_id=baseline.catalog_strategy_id,
            bars=len(bars),
            observations=len(net.returns),
            periods_per_year=periods,
            starting_capital=self.starting_capital,
            final_equity=net.equity_curve[-1] if net.equity_curve else self.starting_capital,
            net_total_return=net_total,
            gross_total_return=gross_total,
            cost_drag=gross_total - net_total,
            expectancy_after_costs=(
                sum(trade_pnls, Decimal(0)) / Decimal(len(trade_pnls)) if trade_pnls else None
            ),
            expectancy_return=(
                sum(trade_returns, Decimal(0)) / Decimal(len(trade_returns))
                if trade_returns
                else None
            ),
            round_trips=len(net.round_trips),
            hit_rate=(
                Decimal(wins) / Decimal(len(trade_pnls)) if trade_pnls else None
            ),
            turnover=(
                net.traded_notional / average_equity
                if net.traded_notional > 0 and average_equity > 0
                else Decimal(0)
            ),
            trades=len(net.trades),
            open_position_at_end=net.open_position_at_end,
            sharpe=metrics.sharpe,
            sortino=metrics.sortino,
            max_drawdown=metrics.max_drawdown,
            mean_return_t_statistic=_t_statistic(significance),
            mean_return_standard_error=(
                None if significance is None else significance.standard_error
            ),
            mean_return_observations=0 if significance is None else significance.observations,
            trade_t_statistic=_t_statistic(trade_significance),
            spread_drag=net.spread_drag,
            slippage_drag=net.slippage_drag,
            cash_charges=net.cash_charges,
            notes=_notes(net, metrics, significance, trade_significance, net_total, gross_total),
        )

    def _run(
        self,
        baseline: PriceOnlyBaseline,
        bars: tuple[Candle, ...],
        instrument: Instrument,
        friction_model: MarketFrictionModel,
    ) -> BaselineRun:
        """One pass down the series, with the decision and the fill on different bars.

        The order for each step is fixed and is the whole no-lookahead argument: bars up to
        ``index - 1``'s close decide the target weight, that weight is sized against equity
        marked at that same close, the fill happens at ``index``'s open, and only then is
        the account re-marked at ``index``'s close. The bar being traded into contributes
        its open and nothing else; its close is not visible to any decision on this step.

        A round trip is flat-to-flat, so a volatility trim that reduces a position without
        closing it does not manufacture a trade for the expectancy to average over.
        """
        cash = self.starting_capital
        shares = 0
        curve: list[Decimal] = [self.starting_capital]
        held: list[int] = [0]
        trades: list[BaselineTrade] = []
        round_trips: list[RoundTrip] = []
        cash_out = Decimal(0)
        cash_in = Decimal(0)
        opened_index = 0
        for index in range(1, len(bars)):
            history = bars[:index]
            execution = bars[index]
            _assert_closed_history(history, execution)
            decision_close = history[-1].close
            equity = cash + Decimal(shares) * decision_close
            held_weight = (
                Decimal(shares) * decision_close / equity if equity > 0 else Decimal(0)
            )
            target = _validated_weight(baseline, baseline.target_weight(history, held_weight))
            desired = (
                0
                if len(history) < baseline.warmup_bars or decision_close <= 0
                else int(target * equity / decision_close)
            )
            delta = desired - shares
            if delta != 0:
                context = self._context(history, instrument)
                side = Side.BUY if delta > 0 else Side.SELL
                filled, cash, trade = self._execute(
                    side,
                    abs(delta),
                    execution,
                    index,
                    instrument,
                    context,
                    friction_model,
                    cash,
                )
                if filled > 0:
                    if shares == 0:
                        opened_index, cash_out, cash_in = index, Decimal(0), Decimal(0)
                    if side == Side.BUY:
                        shares += filled
                        cash_out += trade.notional + trade.cash_charges
                    else:
                        shares -= filled
                        cash_in += trade.notional - trade.cash_charges
                    trades.append(trade)
                    if shares == 0:
                        round_trips.append(RoundTrip(opened_index, index, cash_out, cash_in))
            curve.append(cash + Decimal(shares) * execution.close)
            held.append(shares)
        returns = tuple(
            (after - before) / before for before, after in zip(curve, curve[1:]) if before > 0
        )
        return BaselineRun(
            tuple(curve), returns, tuple(held), tuple(trades), tuple(round_trips), shares > 0
        )

    def _execute(
        self,
        side: Side,
        quantity: int,
        execution: Candle,
        index: int,
        instrument: Instrument,
        context: FrictionContext,
        friction_model: MarketFrictionModel,
        cash: Decimal,
    ) -> tuple[int, Decimal, BaselineTrade | None]:
        """Fill at the execution bar's open, priced and charged by the friction model.

        A buy is trimmed until the account can pay for it. That trim uses the execution
        bar's own open, which is not lookahead: the order was already sized from the last
        closed bar, and this is the broker refusing to overdraw the account at the moment
        of the fill, exactly as the paper ledger refuses it.
        """
        while quantity > 0:
            order = OrderIntent(
                instrument.symbol,
                instrument.market,
                side,
                quantity,
                execution.open,
                "baseline",
                instrument.asset_class,
                self.tenant_id,
            )
            friction = friction_model.evaluate(order, context)
            notional = friction.execution_price * Decimal(quantity)
            outlay = notional + friction.cash_charges
            if side == Side.BUY and outlay > cash:
                affordable = int(cash * Decimal(quantity) / outlay) if outlay > 0 else 0
                quantity = min(quantity - 1, affordable)
                continue
            trade = BaselineTrade(
                index,
                side,
                quantity,
                execution.open,
                friction.execution_price,
                friction.cash_charges,
                friction.spread_drag,
                friction.slippage_drag,
            )
            if side == Side.BUY:
                return quantity, cash - outlay, trade
            return quantity, cash + notional - friction.cash_charges, trade
        return 0, cash, None

    def _context(self, history: tuple[Candle, ...], instrument: Instrument) -> FrictionContext:
        """ATR and ADV from the closed bars this step may see, on the shared cost inputs.

        ``bars_per_session=1`` because one daily bar *is* one session. The default of 390
        belongs to a one-minute series; using it here would multiply the average-daily-volume
        estimate by 390, shrink modelled participation by the same factor, and make every
        baseline look like it traded into infinite depth.
        """
        return friction_context_from_bars(
            history,
            liquidity_score=self.liquidity_score,
            delivery=self.delivery,
            bars_per_session=1,
            observed_half_spread_fraction=self.observed_half_spread_fraction,
        )


def _validated_weight(baseline: PriceOnlyBaseline, weight: Decimal) -> Decimal:
    if not isinstance(weight, Decimal) or not Decimal(0) <= weight <= Decimal(1):
        raise ValueError(
            f"{baseline.baseline_id} returned {weight!r}: baselines are long-only and "
            "unlevered, so a target weight must be a Decimal in [0, 1]"
        )
    return weight


def _assert_closed_history(history: tuple[Candle, ...], execution: Candle) -> None:
    if not history or history[-1].timestamp >= execution.timestamp:
        raise LookaheadError(
            "lookahead_violation: a bar at or after the execution bar was offered to a baseline"
        )


def _assert_daily_bars(bars: tuple[Candle, ...]) -> None:
    if len(bars) < 2:
        raise ValueError("baseline evaluation needs at least two bars")
    if any(
        current.timestamp.tzinfo is None or current.timestamp.utcoffset() is None
        for current in bars
    ):
        raise ValueError("baseline bars must be timezone-aware")
    for previous, current in zip(bars, bars[1:]):
        gap = current.timestamp - previous.timestamp
        if gap < MINIMUM_DAILY_BAR_SPACING:
            raise ValueError(
                f"baseline evaluation expects daily bars; found a {gap} gap at "
                f"{current.timestamp.isoformat()}. Annualising an intraday series against "
                "the daily basis is what inflates a Sharpe; resample or score it elsewhere."
            )
    if any(bar.instrument != bars[0].instrument for bar in bars):
        raise ValueError("baseline evaluation requires one instrument per series")


def _total_return(curve: tuple[Decimal, ...], starting_capital: Decimal) -> Decimal:
    if not curve or starting_capital <= 0:
        return Decimal(0)
    return (curve[-1] - starting_capital) / starting_capital


def _t_statistic(significance: MeanReturnSignificance | None) -> Decimal | None:
    return None if significance is None else significance.t_statistic


def _notes(
    run: BaselineRun,
    metrics: PerformanceMetrics,
    significance: MeanReturnSignificance | None,
    trade_significance: MeanReturnSignificance | None,
    net_total: Decimal,
    gross_total: Decimal,
) -> tuple[str, ...]:
    """Everything the numbers above cannot say about themselves."""
    notes: list[str] = []
    observations = len(run.returns)
    if metrics.sharpe is None:
        notes.append(
            f"Sharpe and Sortino withheld: {observations} observations, below the "
            f"{MINIMUM_RATIO_OBSERVATIONS} a ratio needs to mean anything."
            if observations < MINIMUM_RATIO_OBSERVATIONS
            else "Sharpe and Sortino withheld: the return series has no dispersion. A flat "
            "curve has no risk-adjusted return because it carried no risk, and the 0/0 "
            "behind that ratio is not a zero."
        )
    if significance is None:
        notes.append(
            f"Mean-return t-statistic withheld: {observations} observations, below the "
            f"{MINIMUM_SIGNIFICANCE_OBSERVATIONS} a one-sample t-test needs."
            if observations < MINIMUM_SIGNIFICANCE_OBSERVATIONS
            else "Mean-return t-statistic withheld: the returns have no dispersion, so there "
            "is no standard error to divide the mean by."
        )
    if not run.round_trips:
        notes.append(
            "No closed round trip: expectancy and hit rate are undefined, not zero. "
            "A rule that never completes a trade has not been tested by this window."
        )
    elif len(run.round_trips) < MINIMUM_ROUND_TRIPS:
        notes.append(
            f"{len(run.round_trips)} round trips, below {MINIMUM_ROUND_TRIPS}: the "
            "expectancy and hit rate are descriptive of this window and are not evidence "
            "of an edge."
        )
    if trade_significance is None and run.round_trips:
        notes.append(
            "Per-trade t-statistic withheld: too few closed trades to test the expectancy "
            "against zero."
        )
    if run.open_position_at_end:
        notes.append(
            "A position is still open at the last bar. Its profit is marked, not realised, "
            "and it has not paid its exit friction yet."
        )
    if gross_total > 0 >= net_total:
        notes.append(
            f"Profitable before costs ({gross_total}) and not after ({net_total}). This is "
            "the ordinary outcome for a rule that trades, and the reason turnover is printed."
        )
    if not run.trades:
        notes.append("Never traded: no friction was charged, and none was earned.")
    return tuple(notes)


def format_comparison(reports: tuple[BaselineReport, ...]) -> str:
    """A fixed-width table. A Sharpe is never rendered here without its t-statistic.

    The pairing is the point: printing a ratio alone is how an annualised 6.67 built from a
    mean whose t-statistic was 0.16 got read as a result instead of as noise.
    """
    header = (
        f"{'baseline':<28}{'net_ret':>10}{'gross_ret':>11}{'sharpe':>9}{'t_stat':>9}"
        f"{'sortino':>9}{'max_dd':>9}{'hit':>8}{'turnover':>10}{'trips':>7}{'obs':>6}"
    )
    lines = [header, "-" * len(header)]
    for report in reports:
        lines.append(
            f"{report.baseline_id:<28}"
            f"{_cell(report.net_total_return):>10}"
            f"{_cell(report.gross_total_return):>11}"
            f"{_cell(report.sharpe):>9}"
            f"{_cell(report.mean_return_t_statistic):>9}"
            f"{_cell(report.sortino):>9}"
            f"{_cell(report.max_drawdown):>9}"
            f"{_cell(report.hit_rate):>8}"
            f"{_cell(report.turnover):>10}"
            f"{report.round_trips:>7}"
            f"{report.observations:>6}"
        )
    for report in reports:
        for note in report.notes:
            lines.append(f"  {report.baseline_id}: {note}")
    return "\n".join(lines)


def _cell(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


def baseline_instrument(market: Market) -> Instrument:
    """A plain cash-equity instrument for a series that arrived without one."""
    if market == Market.INDIA:
        return Instrument("BASELINE", Market.INDIA, AssetClass.EQUITY, "INR", "NSE")
    return Instrument("BASELINE", Market.USA, AssetClass.EQUITY, "USD", "NASDAQ")
