from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class Market(str, Enum):
    INDIA = "INDIA"
    USA = "USA"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    market: Market
    side: Side
    quantity: int
    reference_price: Decimal
    strategy_id: str


@dataclass(frozen=True)
class PortfolioSnapshot:
    equity: Decimal
    daily_realized_pnl: Decimal
    gross_exposure: Decimal
