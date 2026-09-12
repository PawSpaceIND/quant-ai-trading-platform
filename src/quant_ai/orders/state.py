from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class OrderState(str, Enum):
    CREATED = "CREATED"
    RISK_APPROVED = "RISK_APPROVED"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


_ALLOWED = {
    OrderState.CREATED: {OrderState.RISK_APPROVED, OrderState.REJECTED, OrderState.CANCELLED},
    OrderState.RISK_APPROVED: {OrderState.SUBMITTED, OrderState.REJECTED, OrderState.CANCELLED},
    OrderState.SUBMITTED: {OrderState.PARTIALLY_FILLED, OrderState.FILLED, OrderState.REJECTED, OrderState.CANCELLED},
    OrderState.PARTIALLY_FILLED: {OrderState.PARTIALLY_FILLED, OrderState.FILLED, OrderState.CANCELLED},
    OrderState.FILLED: set(),
    OrderState.CANCELLED: set(),
    OrderState.REJECTED: set(),
}


@dataclass
class OrderLifecycle:
    state: OrderState = OrderState.CREATED

    def transition(self, new_state: OrderState) -> None:
        if new_state not in _ALLOWED[self.state]:
            raise ValueError(f"invalid order transition {self.state.value}->{new_state.value}")
        self.state = new_state
