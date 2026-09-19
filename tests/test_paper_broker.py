from decimal import Decimal

from quant_ai.domain.models import Market, OrderIntent, Side
from quant_ai.execution.paper_broker import PaperBroker
from quant_ai.execution.paper_fill_lifecycle import (
    PaperFillLifecycleSimulator,
    PaperFillObservation,
)


def test_paper_buy_adds_slippage() -> None:
    order = OrderIntent("AAPL", Market.USA, Side.BUY, 1, Decimal(100), "demo")
    fill = PaperBroker(Decimal(10)).submit(order)
    assert fill.status == "FILLED"
    assert fill.average_price == Decimal("100.100")
    assert fill.order_id.startswith("PAPER-")


def test_default_paper_broker_behavior_remains_immediate_full_fill() -> None:
    order = OrderIntent("INFY", Market.INDIA, Side.BUY, 10, Decimal(1000), "demo")
    broker = PaperBroker(Decimal(1))
    fill = broker.submit(order)
    assert fill.status == "FILLED"
    assert fill.filled_quantity == 10
    assert broker.last_simulation is None


def test_opt_in_lifecycle_returns_partial_fill_and_diagnostics() -> None:
    order = OrderIntent("INFY", Market.INDIA, Side.BUY, 100, Decimal(1000), "demo")
    broker = PaperBroker(
        lifecycle=PaperFillLifecycleSimulator(),
        observation_provider=lambda _order: PaperFillObservation(
            available_quantity=80,
            queue_ahead_quantity=30,
            latency_ms=750,
        ),
    )
    fill = broker.submit(order)
    assert fill.status == "PARTIALLY_FILLED"
    assert fill.filled_quantity == 50
    assert broker.last_simulation is not None
    assert broker.last_simulation.latency_ms == 750
    assert broker.last_simulation.queue_ahead_quantity == 30


def test_opt_in_lifecycle_can_reject_without_fake_fill() -> None:
    order = OrderIntent("INFY", Market.INDIA, Side.BUY, 10, Decimal(1000), "demo")
    broker = PaperBroker(
        lifecycle=PaperFillLifecycleSimulator(),
        observation_provider=lambda _order: PaperFillObservation(
            available_quantity=100,
            rejected=True,
            reject_reason="broker_throttle",
        ),
    )
    fill = broker.submit(order)
    assert fill.status == "REJECTED"
    assert fill.filled_quantity == 0
    assert broker.last_simulation.reject_reason == "broker_throttle"
