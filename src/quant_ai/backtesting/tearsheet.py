from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from decimal import Decimal

from quant_ai.analytics.metrics import (
    MINIMUM_SIGNIFICANCE_OBSERVATIONS,
    alpha_beta,
    maximum_drawdown,
    mean_return_significance,
)
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
    protection_model: str = "not_simulated"
    intrabar_exits: tuple[dict, ...] = ()
    replay_run_id: str | None = None
    # One-sample t-statistic of the mean per-bar return against zero, with the number of
    # observations behind it. None when the curve is too short or has no dispersion.
    mean_return_t_statistic: Decimal | None = None
    return_observations: int = 0
    minimum_significance_observations: int = MINIMUM_SIGNIFICANCE_OBSERVATIONS
    # Cumulative candidate evaluations registered against this study. A tearsheet read
    # without it cannot tell a first look from the fortieth.
    registered_candidate_trials: int | None = None
    registered_runs: int | None = None
    significance_note: str = (
        "The t-statistic tests the mean per-bar return against zero on the observations "
        "shown. It is not corrected for multiple testing; compare it against the "
        "registered candidate-trial count, and note that per-bar returns are serially "
        "correlated."
    )

    def to_json(self) -> str:
        return json.dumps(
            {
                key: str(value) if isinstance(value, Decimal) else value
                for key, value in asdict(self).items()
            },
            sort_keys=True,
        )


def build_tearsheet(
    result: HistoricalReplayResult,
    broker: PaperBrokerService,
    *,
    tenant_id: str = "backtest",
    trial_register: dict | None = None,
) -> TearSheet:
    """Replay summary. ``trial_register`` carries the registered trial counts, if any."""
    curve = result.equity_curve
    initial = broker.get_margin(tenant_id).starting_capital
    final = curve[-1] if curve else initial
    returns = tuple(
        (after - before) / before for before, after in zip(curve, curve[1:]) if before > 0
    )
    alpha, beta = alpha_beta(returns, result.benchmark_returns)
    significance = mean_return_significance(returns)
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
        result.protection_model,
        result.intrabar_exits,
        result.replay_run_id,
        None if significance is None else significance.t_statistic,
        len(returns),
        MINIMUM_SIGNIFICANCE_OBSERVATIONS,
        None if trial_register is None else int(trial_register.get("candidate_trials", 0)),
        None if trial_register is None else int(trial_register.get("runs", 0)),
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
