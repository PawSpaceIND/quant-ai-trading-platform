from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

from quant_ai.brokers.adapter import BrokerPosition
from quant_ai.domain.models import AssetClass, Instrument, Market, PortfolioSnapshot, Side
from quant_ai.execution.paper_ledger import PaperBrokerService, PaperLedgerEntry
from quant_ai.marketdata.feed import MarketDataFeed


@dataclass(frozen=True)
class MarkedPosition:
    symbol: str
    market: Market
    asset_class: AssetClass
    quantity: int
    average_entry_price: Decimal
    current_price: Decimal
    market_value: Decimal
    unrealized_pnl: Decimal


@dataclass(frozen=True)
class PortfolioMetrics:
    cash_balance: Decimal
    positions: tuple[MarkedPosition, ...]
    unrealized_pnl: Decimal
    realized_pnl: Decimal
    daily_realized_pnl: Decimal
    total_equity: Decimal
    high_water_mark: Decimal
    drawdown_fraction: Decimal


class PortfolioTracker:
    """Mark-to-market view over the immutable paper ledger and open paper positions."""

    def __init__(
        self,
        broker: PaperBrokerService,
        market_feed: MarketDataFeed,
        *,
        tenant_id: str = "default",
        instrument_resolver: Callable[[BrokerPosition], Instrument] | None = None,
    ) -> None:
        self.broker = broker
        self.market_feed = market_feed
        self.tenant_id = tenant_id
        self.instrument_resolver = instrument_resolver or self._default_instrument
        # C4: the drawdown circuit breaker is only a breaker if its peak survives a restart.
        # Reload the durable high-water mark; fall back to starting capital on a fresh ledger.
        persisted = broker.get_peak_equity(tenant_id)
        self._high_water_mark = (
            persisted
            if persisted is not None
            else broker.get_margin(tenant_id).starting_capital
        )

    def metrics(self, now: datetime | None = None) -> PortfolioMetrics:
        observed_at = now or datetime.now(timezone.utc)
        margin = self.broker.get_margin(self.tenant_id)
        positions = tuple(self._mark_position(item) for item in self.broker.get_positions(self.tenant_id))
        unrealized = sum((item.unrealized_pnl for item in positions), Decimal(0))
        realized, daily_realized = self._realized_pnl(self.broker.ledger_entries(self.tenant_id), observed_at)
        cash_costs = tuple(
            item for item in self.broker.cost_entries(self.tenant_id) if item.cash_debit
        )
        realized -= sum((item.amount for item in cash_costs), Decimal(0))
        daily_realized -= sum(
            (item.amount for item in cash_costs
             if item.created_at.astimezone(timezone.utc).date()
             == observed_at.astimezone(timezone.utc).date()),
            Decimal(0),
        )
        market_value = sum((item.market_value for item in positions), Decimal(0))
        equity = margin.cash_balance + market_value
        if equity > self._high_water_mark:
            # Persist immediately: a peak that only lives in memory is lost on the next crash.
            self._high_water_mark = self.broker.record_peak_equity(equity, self.tenant_id)
        drawdown = (
            (self._high_water_mark - equity) / self._high_water_mark
            if self._high_water_mark > 0
            else Decimal(0)
        )
        return PortfolioMetrics(
            margin.cash_balance,
            positions,
            unrealized,
            realized,
            daily_realized,
            equity,
            self._high_water_mark,
            max(Decimal(0), drawdown),
        )

    def get_snapshot(self, now: datetime | None = None) -> PortfolioSnapshot:
        metrics = self.metrics(now)
        symbol_exposure = {
            item.symbol: item.market_value for item in metrics.positions
        }
        asset_exposure: dict[AssetClass, Decimal] = {}
        for item in metrics.positions:
            asset_exposure[item.asset_class] = (
                asset_exposure.get(item.asset_class, Decimal(0)) + item.market_value
            )
        gross = sum((item.market_value for item in metrics.positions), Decimal(0))
        return PortfolioSnapshot(
            metrics.total_equity,
            metrics.daily_realized_pnl,
            gross,
            peak_equity=metrics.high_water_mark,
            symbol_exposure=symbol_exposure,
            asset_exposure=asset_exposure,
        )

    def _mark_position(self, position: BrokerPosition) -> MarkedPosition:
        instrument = self.instrument_resolver(position)
        try:
            current_price = self.market_feed.latest_tick(instrument).last_price
        except (RuntimeError, ValueError, TimeoutError):
            current_price = position.average_price
        market_value = current_price * position.quantity
        unrealized = (current_price - position.average_price) * position.quantity
        return MarkedPosition(
            position.symbol,
            position.market,
            position.asset_class,
            position.quantity,
            position.average_price,
            current_price,
            market_value,
            unrealized,
        )

    @staticmethod
    def _realized_pnl(
        entries: tuple[PaperLedgerEntry, ...], now: datetime
    ) -> tuple[Decimal, Decimal]:
        quantity: dict[tuple[str, Market, AssetClass], int] = {}
        average: dict[tuple[str, Market, AssetClass], Decimal] = {}
        realized = Decimal(0)
        daily = Decimal(0)
        for entry in entries:
            key = (entry.symbol, entry.market, entry.asset_class)
            held = quantity.get(key, 0)
            avg = average.get(key, Decimal(0))
            if entry.side == Side.BUY:
                new_qty = held + entry.quantity
                new_avg = ((avg * held) + (entry.fill_price * entry.quantity)) / new_qty
                quantity[key] = new_qty
                average[key] = new_avg
                continue
            pnl = (entry.fill_price - avg) * entry.quantity
            realized += pnl
            if entry.created_at.astimezone(timezone.utc).date() == now.astimezone(timezone.utc).date():
                daily += pnl
            new_qty = held - entry.quantity
            quantity[key] = max(0, new_qty)
            if new_qty <= 0:
                average[key] = Decimal(0)
        return realized, daily

    @staticmethod
    def _default_instrument(position: BrokerPosition) -> Instrument:
        if position.market == Market.INDIA:
            currency, exchange = "INR", "NSE"
        elif position.market == Market.USA:
            currency, exchange = "USD", "NASDAQ"
        else:
            currency, exchange = "USD", "GLOBAL"
        return Instrument(
            position.symbol,
            position.market,
            position.asset_class,
            currency,
            exchange,
        )
