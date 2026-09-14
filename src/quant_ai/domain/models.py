from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum


class Market(str, Enum):
    INDIA = "INDIA"
    USA = "USA"
    GLOBAL = "GLOBAL"


class AssetClass(str, Enum):
    EQUITY = "EQUITY"
    ETF = "ETF"
    INDEX = "INDEX"
    OPTION = "OPTION"
    FUTURE = "FUTURE"
    FX = "FX"
    COMMODITY = "COMMODITY"
    METAL = "METAL"
    BOND = "BOND"
    FUND = "FUND"
    CRYPTO = "CRYPTO"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class RiskMode(str, Enum):
    CONSERVATIVE = "CONSERVATIVE"
    BALANCED = "BALANCED"
    AGGRESSIVE = "AGGRESSIVE"


@dataclass(frozen=True)
class Instrument:
    symbol: str
    market: Market
    asset_class: AssetClass
    currency: str
    exchange: str
    tradable: bool = True
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    market: Market
    side: Side
    quantity: int
    reference_price: Decimal
    strategy_id: str
    asset_class: AssetClass = AssetClass.EQUITY
    tenant_id: str = "default"
    stop_price: Decimal | None = None
    take_profit_price: Decimal | None = None


@dataclass(frozen=True)
class PortfolioSnapshot:
    equity: Decimal
    daily_realized_pnl: Decimal
    gross_exposure: Decimal
    peak_equity: Decimal | None = None
    symbol_exposure: dict[str, Decimal] = field(default_factory=dict)
    asset_exposure: dict[AssetClass, Decimal] = field(default_factory=dict)
    # Held units per symbol. Lets the risk firewall tell an unwind apart from a
    # short open by quantity rather than by mark-vs-reference notional drift.
    symbol_quantity: dict[str, int] = field(default_factory=dict)
    # Mark-to-market change from the persisted start-of-day equity baseline.
    # This is the loss circuit breaker's authoritative intraday P&L input.
    daily_total_pnl: Decimal = Decimal(0)
    # Aggregate marked exposure by country, derived from current open positions.
    country_exposure: dict[str, Decimal] = field(default_factory=dict)


@dataclass(frozen=True)
class Signal:
    source: str
    score: Decimal
    confidence: Decimal
    horizon_minutes: int
    rationale: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class Opportunity:
    instrument: Instrument
    side: Side
    probability: Decimal
    expected_return: Decimal
    expected_loss: Decimal
    expected_value: Decimal
    confidence: Decimal
    stop_distance: Decimal
    take_profit_distance: Decimal
    signals: tuple[Signal, ...]
