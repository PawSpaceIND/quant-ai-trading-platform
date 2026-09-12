from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from random import Random


@dataclass(frozen=True)
class MonteCarloSummary:
    worst_terminal_pnl: Decimal
    median_terminal_pnl: Decimal
    best_terminal_pnl: Decimal
    loss_probability: Decimal


def bootstrap_terminal_pnl(
    trade_pnls: tuple[Decimal, ...],
    simulations: int = 1000,
    seed: int = 7,
) -> MonteCarloSummary:
    if not trade_pnls or simulations <= 0:
        raise ValueError("trade pnl sample and positive simulations are required")
    rng = Random(seed)
    outcomes: list[Decimal] = []
    for _ in range(simulations):
        total = sum((rng.choice(trade_pnls) for _ in trade_pnls), Decimal(0))
        outcomes.append(total)
    outcomes.sort()
    losses = sum(1 for value in outcomes if value < 0)
    return MonteCarloSummary(
        outcomes[0],
        outcomes[len(outcomes) // 2],
        outcomes[-1],
        Decimal(losses) / Decimal(simulations),
    )
