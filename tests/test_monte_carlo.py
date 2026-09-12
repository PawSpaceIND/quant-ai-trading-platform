from decimal import Decimal

from quant_ai.validation.monte_carlo import bootstrap_terminal_pnl


def test_bootstrap_is_reproducible() -> None:
    sample = (Decimal(10), Decimal(-5), Decimal(3), Decimal(4))
    first = bootstrap_terminal_pnl(sample, simulations=100, seed=9)
    second = bootstrap_terminal_pnl(sample, simulations=100, seed=9)
    assert first == second
    assert Decimal(0) <= first.loss_probability <= Decimal(1)
