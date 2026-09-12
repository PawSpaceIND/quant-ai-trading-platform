from decimal import Decimal

from quant_ai.analytics.performance import summarize


def test_summary_metrics() -> None:
    result = summarize((Decimal(100), Decimal(110), Decimal(105), Decimal(120)), (Decimal(10), Decimal(-5), Decimal(15)))
    assert result.total_return == Decimal("0.2")
    assert result.max_drawdown == Decimal(5) / Decimal(110)
    assert result.win_rate == Decimal(2) / Decimal(3)
    assert result.profit_factor == Decimal(5)
