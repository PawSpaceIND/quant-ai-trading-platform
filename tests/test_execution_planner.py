from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant_ai.domain.models import AssetClass, Market, OrderIntent, Side
from quant_ai.execution.planner import (
    ExecutionAlgorithm,
    ExecutionConstraints,
    ExecutionPlanner,
    VolumeBucket,
)

NOW = datetime(2026, 9, 16, 4, tzinfo=timezone.utc)


def order(quantity=100):
    return OrderIntent(
        "INFY", Market.INDIA, Side.BUY, quantity, Decimal(1500), "strategy",
        AssetClass.EQUITY, "tenant",
    )


def buckets(*volumes):
    return tuple(VolumeBucket(NOW + timedelta(minutes=i), volume) for i, volume in enumerate(volumes))


def test_immediate_execution_never_exceeds_observed_participation():
    plan = ExecutionPlanner().plan(
        order(50), algorithm=ExecutionAlgorithm.IMMEDIATE,
        constraints=ExecutionConstraints(max_participation=Decimal("0.10")),
        buckets=buckets(500),
    )
    assert plan.planned_quantity == 50
    assert plan.slices[0].participation == Decimal("0.10")
    with pytest.raises(ValueError, match="immediate_capacity_insufficient"):
        ExecutionPlanner().plan(
            order(51), algorithm=ExecutionAlgorithm.IMMEDIATE,
            constraints=ExecutionConstraints(max_participation=Decimal("0.10")),
            buckets=buckets(500),
        )


def test_twap_is_exact_lot_preserving_and_refuses_thin_buckets():
    constraints = ExecutionConstraints(lot_size=10, max_participation=Decimal("0.20"))
    plan = ExecutionPlanner().plan(
        order(100), algorithm=ExecutionAlgorithm.TWAP, constraints=constraints,
        buckets=buckets(200, 200, 200, 200, 200),
    )
    assert [item.quantity for item in plan.slices] == [20, 20, 20, 20, 20]
    assert plan.planned_quantity == 100
    with pytest.raises(ValueError, match="twap_capacity_insufficient"):
        ExecutionPlanner().plan(
            order(100), algorithm=ExecutionAlgorithm.TWAP, constraints=constraints,
            buckets=buckets(200, 200, 40, 200, 200),
        )


def test_volume_weighted_and_pov_stop_when_market_capacity_is_insufficient():
    constraints = ExecutionConstraints(lot_size=10, max_participation=Decimal("0.10"))
    for algorithm in (ExecutionAlgorithm.VWAP, ExecutionAlgorithm.POV):
        plan = ExecutionPlanner().plan(
            order(100), algorithm=algorithm, constraints=constraints,
            buckets=buckets(500, 300, 200),
        )
        assert [item.quantity for item in plan.slices] == [50, 30, 20]
        assert all(item.participation <= Decimal("0.10") for item in plan.slices)
        with pytest.raises(ValueError, match="participation_capacity_insufficient"):
            ExecutionPlanner().plan(
                order(110), algorithm=algorithm, constraints=constraints,
                buckets=buckets(500, 300, 200),
            )


def test_child_size_and_minimum_constraints_are_fail_closed():
    planner = ExecutionPlanner()
    with pytest.raises(ValueError, match="max_child_quantity_must_be_whole_lots"):
        ExecutionConstraints(lot_size=10, max_child_quantity=25)
    constraints = ExecutionConstraints(
        lot_size=10, max_participation=Decimal("0.5"), max_child_quantity=30,
        min_child_quantity=20,
    )
    plan = planner.plan(
        order(60), algorithm=ExecutionAlgorithm.POV, constraints=constraints,
        buckets=buckets(100, 100),
    )
    assert [item.quantity for item in plan.slices] == [30, 30]
    assert all(20 <= item.quantity <= 30 for item in plan.slices)


def test_missing_volume_out_of_order_time_and_fractional_lots_never_get_planned():
    planner = ExecutionPlanner()
    with pytest.raises(ValueError, match="volume_schedule_required"):
        planner.plan(order(), algorithm=ExecutionAlgorithm.TWAP, constraints=ExecutionConstraints())
    with pytest.raises(ValueError, match="parent_quantity_not_whole_lots"):
        planner.plan(
            order(95), algorithm=ExecutionAlgorithm.TWAP,
            constraints=ExecutionConstraints(lot_size=10), buckets=buckets(1000, 1000),
        )
    reversed_buckets = tuple(reversed(buckets(1000, 1000)))
    with pytest.raises(ValueError, match="strictly_ordered"):
        planner.plan(
            order(), algorithm=ExecutionAlgorithm.TWAP,
            constraints=ExecutionConstraints(), buckets=reversed_buckets,
        )
