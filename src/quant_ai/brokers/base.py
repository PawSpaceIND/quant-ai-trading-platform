from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal

from quant_ai.domain.models import OrderIntent


@dataclass(frozen=True)
class ExecutionResult:
    order_id: str
    status: str
    filled_quantity: int
    average_price: Decimal


class Broker(ABC):
    @abstractmethod
    def submit(self, order: OrderIntent) -> ExecutionResult:
        raise NotImplementedError
