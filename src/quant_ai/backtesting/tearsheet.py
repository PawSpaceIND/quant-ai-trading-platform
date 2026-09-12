from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from decimal import Decimal

from quant_ai.analytics.metrics import alpha_beta, maximum_drawdown
from quant_ai.backtesting.replay import HistoricalReplayResult
from quant_ai.execution.paper_ledger import PaperBrokerService


@dataclass(frozen=True)
class TearSheet:
    total_net_return: Decimal
    alpha: Decimal
    beta: Decimal
    max_drawdown: Decimal
    max_drawdown_duration_bars: int
    cumulative_statutory_fees: Decimal
    realized_slippage_drag: Decimal
    realized_spread_drag: Decimal
    trades: int

    def to_json(self) -> str:
        return json.dumps(
            {key: str(value) if isinstance(value, Decimal) else value for key, value in asdict(self).items()},
            sort_keys=True,
        )


def build_tearsheet(
    result: HistoricalReplayResult,
    broker: PaperBrokerService,
    *,
    tenant_id: str = "backtest",
) -> TearSheet:
    curve = result.equity_curve
    initial = broker.get_margin(tenant_id).starting_capital
    final = curve[-1] if curve else initial
    returns = tuple(
        (after - before) / before for before, after in zip(curve, curve[1:]) if before > 0
    )
    alpha, beta = alpha_beta(returns, result.benchmark_returns)
    costs = broker.cost_entries(tenant_id)
    statutory = sum((item.amount for item in costs if item.cash_debit), Decimal(0))
    slippage = sum((item.amount for item in costs if item.code == "SLIPPAGE"), Decimal(0))
    spread = sum((item.amount for item in costs if item.code == "SPREAD"), Decimal(0))
    return TearSheet(
        (final - initial) / initial if initial > 0 else Decimal(0),
        alpha,
        beta,
        maximum_drawdown(curve),
        _max_drawdown_duration(curve),
        statutory,
        slippage,
        spread,
        len(result.order_ids),
    )


def _max_drawdown_duration(curve: tuple[Decimal, ...]) -> int:
    peak = Decimal("-Infinity")
    duration = 0
    worst = 0
    for value in curve:
        if value >= peak:
            peak = value
            duration = 0
        else:
            duration += 1
            worst = max(worst, duration)
    return worst
