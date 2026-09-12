from decimal import Decimal

from quant_ai.risk.stress import StressPosition, equity_shock


def test_negative_market_shock_estimates_loss() -> None:
    result = equity_shock((StressPosition("A", Decimal(50000)),), Decimal(100000), Decimal("-0.10"))
    assert result.estimated_pnl == Decimal(-5000)
    assert result.estimated_loss_fraction == Decimal("0.05")
