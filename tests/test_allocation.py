from decimal import Decimal

from quant_ai.portfolio.allocation import AllocationInput, RiskParityAllocator


def test_allocations_sum_to_one() -> None:
    weights = RiskParityAllocator().allocate((
        AllocationInput("A", Decimal(1), Decimal("0.2")),
        AllocationInput("B", Decimal(1), Decimal("0.1")),
    ))
    assert sum(weights.values(), Decimal(0)).quantize(Decimal("0.0001")) == Decimal("1.0000")
    assert weights["B"] > weights["A"]
