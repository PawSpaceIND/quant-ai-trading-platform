from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class PerformanceSummary:
    total_return: Decimal
    max_drawdown: Decimal
    win_rate: Decimal
    profit_factor: Decimal
    trades: int


def summarize(equity_curve: tuple[Decimal, ...], trade_pnls: tuple[Decimal, ...]) -> PerformanceSummary:
    if len(equity_curve) < 2 or equity_curve[0] <= 0:
        raise ValueError("valid equity curve required")
    peak = equity_curve[0]
    max_dd = Decimal(0)
    for value in equity_curve:
        peak = max(peak, value)
        dd = (peak - value) / peak
        max_dd = max(max_dd, dd)
    wins = [p for p in trade_pnls if p > 0]
    losses = [p for p in trade_pnls if p < 0]
    total_return = equity_curve[-1] / equity_curve[0] - Decimal(1)
    win_rate = Decimal(len(wins)) / Decimal(len(trade_pnls)) if trade_pnls else Decimal(0)
    gross_profit = sum(wins, Decimal(0))
    gross_loss = abs(sum(losses, Decimal(0)))
    profit_factor = gross_profit / gross_loss if gross_loss else (Decimal("Infinity") if gross_profit else Decimal(0))
    return PerformanceSummary(total_return, max_dd, win_rate, profit_factor, len(trade_pnls))
