from decimal import Decimal

import pytest

from quant_ai.brokers.simulated import SimulatedBroker, SimulatedBrokerMode
from quant_ai.domain.models import Market, OrderIntent, Side


def order() -> OrderIntent:
    return OrderIntent("AAPL", Market.USA, Side.BUY, 10, Decimal(100), "test", stop_price=Decimal(95))


def test_failure_modes() -> None:
    assert SimulatedBroker(SimulatedBrokerMode.REJECT).submit(order()).status == "REJECTED"
    assert SimulatedBroker(SimulatedBrokerMode.PARTIAL_FILL).submit(order()).status == "PARTIAL"
    with pytest.raises(TimeoutError):
        SimulatedBroker(SimulatedBrokerMode.TIMEOUT).submit(order())
