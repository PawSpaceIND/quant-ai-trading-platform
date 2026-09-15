from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_CEILING, Decimal
from math import sqrt
from statistics import mean, pstdev, stdev

RATIO_CAP = Decimal(20)

# Sessions in a trading year: the unit an annualised ratio is quoted in.
TRADING_DAYS_PER_YEAR = 252

# An annualised ratio built from a handful of observations is sampling noise wearing a
# number's clothes. Below this count no ratio is reported at all.
MINIMUM_RATIO_OBSERVATIONS = 30

# The same floor for a one-sample t-statistic on a mean return.
MINIMUM_SIGNIFICANCE_OBSERVATIONS = 30


@dataclass(frozen=True)
class PerformanceMetrics:
    # None when the series is too short for the ratio to carry any meaning.
    sharpe: Decimal | None
    sortino: Decimal | None
    max_drawdown: Decimal
    win_loss_ratio: Decimal
    var_95: Decimal
    alpha: Decimal
    beta: Decimal
    # Periods per year the ratios were annualised with, so a reader can tell a daily
    # Sharpe from a one-minute one instead of assuming.
    periods_per_year: int = TRADING_DAYS_PER_YEAR
    observations: int = 0


@dataclass(frozen=True)
class MeanReturnSignificance:
    """One-sample t-test of a mean return against zero.

    ``t_statistic`` is ``mean / (sample stdev / sqrt(n))`` on the raw, unannualised
    observations. It is not corrected for multiple testing: it says nothing about how many
    candidate strategies, windows or parameter settings were tried before this one was
    reported. Serially correlated observations inflate it further.
    """

    observations: int
    mean: Decimal
    standard_error: Decimal
    t_statistic: Decimal
    multiple_testing_correction: str = "none"


def _d(value: float) -> Decimal:
    return Decimal(str(value))


def _bounded(value: Decimal, limit: Decimal = RATIO_CAP) -> Decimal:
    return min(limit, max(-limit, value))


def annualisation_periods(
    interval: timedelta,
    session: timedelta,
    trading_days: int = TRADING_DAYS_PER_YEAR,
) -> int:
    """Sampling intervals in a trading year for a series sampled every ``interval``.

    ``session`` is the length of one regular trading session, so a one-minute series on a
    375-minute session annualises with 375 * ``trading_days`` periods, not ``trading_days``.
    """
    step = interval.total_seconds()
    span = session.total_seconds()
    if step <= 0 or span <= 0 or trading_days <= 0:
        raise ValueError("annualisation needs a positive interval, session and year")
    per_session = int(span // step)
    if per_session < 1:
        raise ValueError("sampling interval is longer than one session")
    return per_session * trading_days


def sharpe_ratio(
    returns: tuple[Decimal, ...],
    risk_free_rate: Decimal = Decimal(0),
    periods: int = TRADING_DAYS_PER_YEAR,
    *,
    minimum_observations: int = MINIMUM_RATIO_OBSERVATIONS,
) -> Decimal | None:
    """Annualised Sharpe, or None when the series cannot support one.

    ``periods`` must be the number of sampling intervals in a trading year for *these*
    returns - see :func:`annualisation_periods`. Passing the daily default for a
    one-minute series is what overstates the ratio.
    """
    if periods <= 0 or len(returns) < max(2, minimum_observations):
        return None
    excess = [float(item - risk_free_rate / Decimal(periods)) for item in returns]
    sigma = pstdev(excess)
    return _bounded(_d(mean(excess) / sigma * sqrt(periods))) if sigma > 0 else Decimal(0)


def sortino_ratio(
    returns: tuple[Decimal, ...],
    minimum_acceptable_return: Decimal = Decimal(0),
    periods: int = TRADING_DAYS_PER_YEAR,
    *,
    minimum_observations: int = MINIMUM_RATIO_OBSERVATIONS,
) -> Decimal | None:
    """Annualised Sortino, or None when the series cannot support one."""
    if periods <= 0 or len(returns) < max(2, minimum_observations):
        return None
    target = float(minimum_acceptable_return / Decimal(periods))
    values = [float(item) for item in returns]
    downside = [min(0.0, item - target) for item in values]
    downside_deviation = sqrt(sum(item * item for item in downside) / len(downside))
    return _bounded(_d((mean(values) - target) / downside_deviation * sqrt(periods))) if downside_deviation > 0 else Decimal(0)


def mean_return_significance(
    returns: tuple[Decimal, ...],
    *,
    minimum_observations: int = MINIMUM_SIGNIFICANCE_OBSERVATIONS,
) -> MeanReturnSignificance | None:
    """One-sample t-statistic for the mean of ``returns``, or None when it cannot be one.

    None below ``minimum_observations`` and whenever the sample has no dispersion, so a
    caller never renders a t-statistic that no sample supports.
    """
    if len(returns) < max(2, minimum_observations):
        return None
    values = [float(item) for item in returns]
    dispersion = stdev(values)
    if dispersion <= 0:
        return None
    average = mean(values)
    error = dispersion / sqrt(len(values))
    return MeanReturnSignificance(
        len(values),
        _d(average),
        _d(error),
        _bounded(_d(average / error), Decimal(1000)),
    )


def maximum_drawdown(equity_curve: tuple[Decimal, ...]) -> Decimal:
    if not equity_curve:
        return Decimal(0)
    peak = equity_curve[0]
    worst = Decimal(0)
    for value in equity_curve:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return worst


def win_loss_ratio(pnls: tuple[Decimal, ...]) -> Decimal:
    wins = sum(1 for item in pnls if item > 0)
    losses = sum(1 for item in pnls if item < 0)
    if losses == 0:
        return min(Decimal(100), Decimal(wins)) if wins else Decimal(0)
    return min(Decimal(100), Decimal(wins) / Decimal(losses))


def historical_var(returns: tuple[Decimal, ...], confidence: Decimal = Decimal("0.95")) -> Decimal:
    if not returns or not Decimal(0) < confidence < Decimal(1):
        return Decimal(0)
    ordered = sorted(returns)
    tail = Decimal(1) - confidence
    index = min(len(ordered) - 1, max(0, int((Decimal(len(ordered)) * tail).to_integral_value(rounding=ROUND_CEILING)) - 1))
    return min(Decimal(1), max(Decimal(0), -ordered[index]))


def alpha_beta(returns: tuple[Decimal, ...], benchmark: tuple[Decimal, ...]) -> tuple[Decimal, Decimal]:
    count = min(len(returns), len(benchmark))
    if count < 2:
        return Decimal(0), Decimal(0)
    y = [float(item) for item in returns[-count:]]
    x = [float(item) for item in benchmark[-count:]]
    mx, my = mean(x), mean(y)
    variance = sum((item - mx) ** 2 for item in x)
    if variance == 0:
        return Decimal(0), Decimal(0)
    beta = sum((a - mx) * (b - my) for a, b in zip(x, y)) / variance
    alpha = my - beta * mx
    return _bounded(_d(alpha), Decimal(1)), _bounded(_d(beta), Decimal(10))


def summarize_performance(
    returns: tuple[Decimal, ...],
    equity_curve: tuple[Decimal, ...],
    pnls: tuple[Decimal, ...],
    benchmark: tuple[Decimal, ...] = (),
    *,
    periods: int = TRADING_DAYS_PER_YEAR,
    minimum_observations: int = MINIMUM_RATIO_OBSERVATIONS,
) -> PerformanceMetrics:
    """Summary of a return series. ``periods`` must match how often ``returns`` is sampled."""
    alpha, beta = alpha_beta(returns, benchmark)
    return PerformanceMetrics(
        sharpe_ratio(returns, periods=periods, minimum_observations=minimum_observations),
        sortino_ratio(returns, periods=periods, minimum_observations=minimum_observations),
        maximum_drawdown(equity_curve),
        win_loss_ratio(pnls),
        historical_var(returns),
        alpha,
        beta,
        periods,
        len(returns),
    )
