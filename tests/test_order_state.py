import pytest

from quant_ai.orders.state import OrderLifecycle, OrderState


def test_order_lifecycle_happy_path() -> None:
    lifecycle = OrderLifecycle()
    lifecycle.transition(OrderState.RISK_APPROVED)
    lifecycle.transition(OrderState.SUBMITTED)
    lifecycle.transition(OrderState.FILLED)
    assert lifecycle.state == OrderState.FILLED


def test_terminal_state_cannot_reopen() -> None:
    lifecycle = OrderLifecycle(OrderState.FILLED)
    with pytest.raises(ValueError):
        lifecycle.transition(OrderState.SUBMITTED)
