from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class TenantPosition:
    tenant_id: str
    symbol: str
    quantity: int
    market_value: Decimal


class TenantPortfolioStore:
    def __init__(self) -> None:
        self._positions: dict[tuple[str, str], TenantPosition] = {}

    def upsert(self, position: TenantPosition) -> None:
        if position.quantity < 0 or position.market_value < 0:
            raise ValueError("invalid position")
        self._positions[(position.tenant_id, position.symbol)] = position

    def list_for_tenant(self, tenant_id: str) -> tuple[TenantPosition, ...]:
        rows = [position for (tenant, _), position in self._positions.items() if tenant == tenant_id]
        return tuple(sorted(rows, key=lambda position: position.symbol))
