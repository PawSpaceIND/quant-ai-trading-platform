from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

from quant_ai.brokers.adapter import BrokerPosition
from quant_ai.domain.models import AssetClass, Instrument, Market, PortfolioSnapshot, Side
from quant_ai.execution.derivative_margin import MARGINED_FUTURES_ASSET_CLASSES
from quant_ai.execution.ledger_integrity import finite_amount
from quant_ai.execution.paper_ledger import PaperBrokerService, PaperLedgerEntry
from quant_ai.execution.risk_state import RiskStateStore, risk_state_for_broker
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
    reserved_margin: Decimal = Decimal(0)


@dataclass(frozen=True)
class PortfolioMetrics:
    cash_balance: Decimal
    positions: tuple[MarkedPosition, ...]
    unrealized_pnl: Decimal
    realized_pnl: Decimal
    daily_realized_pnl: Decimal
    daily_total_pnl: Decimal
    total_equity: Decimal
    high_water_mark: Decimal
    drawdown_fraction: Decimal
    reserved_margin: Decimal = Decimal(0)


class PortfolioTracker:
    """Mark-to-market view over the immutable paper ledger and open paper positions."""

    def __init__(
        self,
        broker: PaperBrokerService,
        market_feed: MarketDataFeed,
        *,
        tenant_id: str = "default",
        instrument_resolver: Callable[[BrokerPosition], Instrument] | None = None,
        risk_state: RiskStateStore | None = None,
    ) -> None:
        self.broker = broker
        self.market_feed = market_feed
        self.tenant_id = tenant_id
        self._fallback_instrument_resolver = instrument_resolver or self._default_instrument
        self.instrument_resolver = self._resolve_instrument
        starting_capital = broker.get_starting_capital(tenant_id)
        self.risk_state = risk_state or risk_state_for_broker(
            broker,
            starting_capital=starting_capital,
        )
        persisted = broker.get_peak_equity(tenant_id)
        self._high_water_mark = persisted if persisted is not None else starting_capital

    def metrics(self, now: datetime | None = None) -> PortfolioMetrics:
        with self.broker._lock:
            return self._metrics_locked(now)

    def _metrics_locked(self, now: datetime | None = None) -> PortfolioMetrics:
        observed_at = now or datetime.now(timezone.utc)
        margin = self.broker.get_margin(self.tenant_id)
        positions = tuple(
            self._mark_position(item) for item in self.broker.get_positions(self.tenant_id)
        )
        unrealized = sum((item.unrealized_pnl for item in positions), Decimal(0))
        realized, daily_realized = self._realized_pnl(
            self.broker.ledger_entries(self.tenant_id), observed_at
        )
        cash_costs = tuple(
            item for item in self.broker.cost_entries(self.tenant_id) if item.cash_debit
        )
        realized -= sum((item.amount for item in cash_costs), Decimal(0))
        daily_realized -= sum(
            (
                item.amount
                for item in cash_costs
                if item.created_at.astimezone(timezone.utc).date()
                == observed_at.astimezone(timezone.utc).date()
            ),
            Decimal(0),
        )
        # Cash instruments contribute their marked value to equity. Futures contribute
        # only the cash already reserved as margin plus unrealised P&L; adding their full
        # notional here would pretend the account owns the underlying commodity outright.
        reserved_margin = sum((item.reserved_margin for item in positions), Decimal(0))
        equity_value = sum(
            (
                item.reserved_margin + item.unrealized_pnl
                if item.asset_class in MARGINED_FUTURES_ASSET_CLASSES
                else item.market_value
                for item in positions
            ),
            Decimal(0),
        )
        equity = finite_amount(margin.cash_balance + equity_value, "invalid_portfolio_equity")
        finite_amount(unrealized, "invalid_portfolio_pnl")
        finite_amount(realized, "invalid_portfolio_pnl")
        finite_amount(daily_realized, "invalid_portfolio_pnl")
        finite_amount(self._high_water_mark, "invalid_equity_peak", positive=True)
        opening_equity = self.risk_state.record_equity(
            self.tenant_id,
            observed_at.astimezone(timezone.utc).date(),
            equity,
        )
        opening_equity = finite_amount(opening_equity, "invalid_opening_equity")
        daily_total = equity - opening_equity
        if equity > self._high_water_mark:
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
            daily_total,
            equity,
            self._high_water_mark,
            max(Decimal(0), drawdown),
            reserved_margin,
        )

    def get_snapshot(self, now: datetime | None = None) -> PortfolioSnapshot:
        metrics = self.metrics(now)
        symbol_exposure = {item.symbol: item.market_value for item in metrics.positions}
        asset_exposure: dict[AssetClass, Decimal] = {}
        country_exposure: dict[str, Decimal] = {}
        for item in metrics.positions:
            asset_exposure[item.asset_class] = (
                asset_exposure.get(item.asset_class, Decimal(0)) + item.market_value
            )
            country = self._country_for_market(item.market)
            country_exposure[country] = (
                country_exposure.get(country, Decimal(0)) + item.market_value
            )
        gross = sum((item.market_value for item in metrics.positions), Decimal(0))
        return PortfolioSnapshot(
            metrics.total_equity,
            metrics.daily_realized_pnl,
            gross,
            peak_equity=metrics.high_water_mark,
            symbol_exposure=symbol_exposure,
            asset_exposure=asset_exposure,
            symbol_quantity={item.symbol: item.quantity for item in metrics.positions},
            daily_total_pnl=metrics.daily_total_pnl,
            country_exposure=country_exposure,
            available_margin=metrics.cash_balance,
        )

    def _mark_position(self, position: BrokerPosition) -> MarkedPosition:
        instrument = self.instrument_resolver(position)
        try:
            current_price = self.market_feed.latest_tick(instrument).last_price
        except (RuntimeError, ValueError, TimeoutError):
            current_price = position.average_price
        current_price = finite_amount(current_price, "invalid_market_mark", positive=True)
        market_value = current_price * position.quantity
        unrealized = (current_price - position.average_price) * position.quantity
        reserved_margin = (
            self.broker.reserved_margin_for(
                position.symbol, position.market, position.asset_class, self.tenant_id
            )
            if position.asset_class in MARGINED_FUTURES_ASSET_CLASSES
            else Decimal(0)
        )
        return MarkedPosition(
            position.symbol,
            position.market,
            position.asset_class,
            position.quantity,
            position.average_price,
            current_price,
            market_value,
            unrealized,
            reserved_margin,
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
            if (
                entry.created_at.astimezone(timezone.utc).date()
                == now.astimezone(timezone.utc).date()
            ):
                daily += pnl
            new_qty = held - entry.quantity
            quantity[key] = max(0, new_qty)
            if new_qty <= 0:
                average[key] = Decimal(0)
        return realized, daily

    @staticmethod
    def _country_for_market(market: Market) -> str:
        if market == Market.USA:
            return "USA"
        if market == Market.INDIA:
            return "India"
        return "Global"

    def _resolve_instrument(self, position: BrokerPosition) -> Instrument:
        # A persisted position's own snapshot wins over a mutable watchlist/catalog.
        try:
            instrument = self.broker.bound_instrument_for_position(
                position.symbol, position.market, position.asset_class, position.tenant_id
            )
        except KeyError:
            # Daemon callers also resolve prospective, not-yet-held cash instruments.
            instrument = None
        return instrument if instrument is not None else self._fallback_instrument_resolver(position)

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
