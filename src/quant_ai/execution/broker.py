from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from quant_ai.brokers.base import ExecutionResult
from quant_ai.domain.models import OrderIntent


@dataclass(frozen=True)
class BrokerAccountSummary:
    tenant_id: str
    currency: str
    cash_balance: Decimal
    net_liquidation: Decimal
    available_margin: Decimal
    raw: dict[str, Any] | None = None


class AbstractBrokerGateway(ABC):
    """Common broker boundary for paper and read-only live-connected adapters."""

    @abstractmethod
    def get_account_summary(self, tenant_id: str = "default") -> BrokerAccountSummary:
        raise NotImplementedError

    @abstractmethod
    def get_positions(self, tenant_id: str = "default") -> tuple[Any, ...]:
        raise NotImplementedError

    @abstractmethod
    def submit_order(self, order: OrderIntent) -> ExecutionResult:
        raise NotImplementedError

    @abstractmethod
    def cancel_order(self, order_id: str, tenant_id: str = "default") -> bool:
        raise NotImplementedError
