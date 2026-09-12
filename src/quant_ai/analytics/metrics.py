from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from math import sqrt
from statistics import mean, pstdev

RATIO_CAP = Decimal(20)


@dataclass(frozen=True)
class PerformanceMetrics:
    sharpe: Decimal
    sortino: Decimal
    max_drawdown: Decimal
    win_loss_ratio: Decimal
    var_95: Decimal
    alpha: Decimal
    beta: Decimal


def _d(value: float) -> Decimal:
    return Decimal(str(value))


def _bounded(value: Decimal, limit: Decimal = RATIO_CAP) -> Decimal:
    return min(limit, max(-limit, value))


def sharpe_ratio(returns: tuple[Decimal, ...], risk_free_rate: Decimal = Decimal(0), periods: int = 252) -> Decimal:
    if periods <= 0 or len(returns) < 2:
        return Decimal(0)
    excess = [float(item - risk_free_rate / Decimal(periods)) for item in returns]
    sigma = pstdev(excess)
    return _bounded(_d(mean(excess) / sigma * sqrt(periods))) if sigma > 0 else Decimal(0)


def sortino_ratio(returns: tuple[Decimal, ...], minimum_acceptable_return: Decimal = Decimal(0), periods: int = 252) -> Decimal:
    if periods <= 0 or not returns:
        return Decimal(0)
    target = float(minimum_acceptable_return / Decimal(periods))
    values = [float(item) for item in returns]
    downside = [min(0.0, item - target) for item in values]
    downside_deviation = sqrt(sum(item * item for item in downside) / len(downside))
    return _bounded(_d((mean(values) - target) / downside_deviation * sqrt(periods))) if downside_deviation > 0 else Decimal(0)


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


def summarize_performance(returns: tuple[Decimal, ...], equity_curve: tuple[Decimal, ...], pnls: tuple[Decimal, ...], benchmark: tuple[Decimal, ...] = ()) -> PerformanceMetrics:
    alpha, beta = alpha_beta(returns, benchmark)
    return PerformanceMetrics(
        sharpe_ratio(returns),
        sortino_ratio(returns),
        maximum_drawdown(equity_curve),
        win_loss_ratio(pnls),
        historical_var(returns),
        alpha,
        beta,
    )
