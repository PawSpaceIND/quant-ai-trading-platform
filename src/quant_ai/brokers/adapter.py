from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal

from quant_ai.brokers.base import Broker, ExecutionResult
from quant_ai.domain.models import AssetClass, Market, OrderIntent, Side
from quant_ai.execution.broker import AbstractBrokerGateway, BrokerAccountSummary


@dataclass(frozen=True)
class BrokerPosition:
    tenant_id: str
    symbol: str
    market: Market
    asset_class: AssetClass
    quantity: int
    average_price: Decimal


@dataclass(frozen=True)
class BrokerMargin:
    tenant_id: str
    starting_capital: Decimal
    cash_balance: Decimal
    gross_position_value: Decimal
    available_margin: Decimal


class BrokerAdapter(Broker, AbstractBrokerGateway, ABC):
    @abstractmethod
    def buy(self, order: OrderIntent) -> ExecutionResult:
        raise NotImplementedError

    @abstractmethod
    def sell(self, order: OrderIntent) -> ExecutionResult:
        raise NotImplementedError

    @abstractmethod
    def cancel(self, order_id: str, tenant_id: str = "default") -> bool:
        raise NotImplementedError

    @abstractmethod
    def get_positions(self, tenant_id: str = "default") -> tuple[BrokerPosition, ...]:
        raise NotImplementedError

    @abstractmethod
    def get_margin(self, tenant_id: str = "default") -> BrokerMargin:
        raise NotImplementedError

    def submit(self, order: OrderIntent) -> ExecutionResult:
        return self.buy(order) if order.side == Side.BUY else self.sell(order)

    def submit_order(self, order: OrderIntent) -> ExecutionResult:
        return self.submit(order)

    def cancel_order(self, order_id: str, tenant_id: str = "default") -> bool:
        return self.cancel(order_id, tenant_id)

    def get_account_summary(self, tenant_id: str = "default") -> BrokerAccountSummary:
        margin = self.get_margin(tenant_id)
        net_liquidation = margin.cash_balance + margin.gross_position_value
        return BrokerAccountSummary(
            tenant_id=tenant_id,
            currency="PAPER",
            cash_balance=margin.cash_balance,
            net_liquidation=net_liquidation,
            available_margin=margin.available_margin,
        )
