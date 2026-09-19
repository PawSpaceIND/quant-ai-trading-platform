from decimal import Decimal

import pytest

from quant_ai.domain.models import Market, OrderIntent, Side
from quant_ai.execution.paper_fill_lifecycle import (
    PaperFillLifecycleSimulator,
    PaperFillObservation,
)


def order(quantity=100, side=Side.BUY):
    return OrderIntent("INFY", Market.INDIA, side, quantity, Decimal(1000), "test")


def test_queue_and_available_volume_create_partial_fill():
    simulation = PaperFillLifecycleSimulator().simulate(
        order(100),
        PaperFillObservation(available_quantity=80, queue_ahead_quantity=30),
        order_id="PAPER-1",
    )
    assert simulation.result.status == "PARTIALLY_FILLED"
    assert simulation.result.filled_quantity == 50
    assert simulation.fillable_quantity == 50


def test_no_capacity_leaves_order_submitted_without_fake_fill():
    simulation = PaperFillLifecycleSimulator().simulate(
        order(100),
        PaperFillObservation(available_quantity=40, queue_ahead_quantity=40),
        order_id="PAPER-2",
    )
    assert simulation.result.status == "SUBMITTED"
    assert simulation.result.filled_quantity == 0
    assert simulation.result.average_price == Decimal(1000)


def test_explicit_rejection_never_mutates_quantity_or_price():
    simulation = PaperFillLifecycleSimulator().simulate(
        order(25),
        PaperFillObservation(
            available_quantity=100,
            rejected=True,
            reject_reason="venue_throttle",
        ),
        order_id="PAPER-3",
    )
    assert simulation.result.status == "REJECTED"
    assert simulation.result.filled_quantity == 0
    assert simulation.reject_reason == "venue_throttle"


def test_latency_penalty_is_adverse_and_deterministic():
    simulator = PaperFillLifecycleSimulator(
        base_slippage_bps=Decimal(1),
        latency_penalty_bps_per_second=Decimal(2),
    )
    fast = simulator.simulate(
        order(10, Side.BUY),
        PaperFillObservation(available_quantity=10, latency_ms=0),
        order_id="FAST",
    )
    slow = simulator.simulate(
        order(10, Side.BUY),
        PaperFillObservation(available_quantity=10, latency_ms=1500),
        order_id="SLOW",
    )
    assert slow.result.status == "FILLED"
    assert slow.result.average_price > fast.result.average_price

    sell_fast = simulator.simulate(
        order(10, Side.SELL),
        PaperFillObservation(available_quantity=10, latency_ms=0),
        order_id="SELL-FAST",
    )
    sell_slow = simulator.simulate(
        order(10, Side.SELL),
        PaperFillObservation(available_quantity=10, latency_ms=1500),
        order_id="SELL-SLOW",
    )
    assert sell_slow.result.average_price < sell_fast.result.average_price


@pytest.mark.parametrize(
    "observation",
    [
        PaperFillObservation(available_quantity=0),
        PaperFillObservation(available_quantity=1, queue_ahead_quantity=1),
    ],
)
def test_zero_fill_never_claims_filled_status(observation):
    result = PaperFillLifecycleSimulator().simulate(
        order(10), observation, order_id="ZERO"
    ).result
    assert result.filled_quantity == 0
    assert result.status != "FILLED"


def test_invalid_microstructure_inputs_fail_closed():
    with pytest.raises(ValueError):
        PaperFillObservation(available_quantity=-1)
    with pytest.raises(ValueError):
        PaperFillObservation(available_quantity=1, queue_ahead_quantity=-1)
    with pytest.raises(ValueError):
        PaperFillObservation(available_quantity=1, latency_ms=-1)
    with pytest.raises(ValueError, match="reject_reason"):
        PaperFillObservation(available_quantity=1, rejected=True)
