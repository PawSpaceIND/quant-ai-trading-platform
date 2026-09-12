from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from quant_ai.brokers.adapter import BrokerAdapter, BrokerMargin, BrokerPosition
from quant_ai.brokers.base import ExecutionResult
from quant_ai.domain.models import AssetClass, Market, OrderIntent, Side
from quant_ai.execution.broker import BrokerAccountSummary
from quant_ai.execution.live_guard import LiveTradingDisabled
from quant_ai.execution.paper_ledger import PaperBrokerService

logger = logging.getLogger(__name__)

# Compile-time firewall: this release has no live-order network path.
LIVE_ORDER_NETWORK_REQUESTS_ENABLED = False


def _env_requests_live_money() -> bool:
    return os.getenv("TRADING_LIVE_MONEY_ACTIVE", "false").strip().lower() == "true"


def _decimal(value: Any, default: str = "0") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    return Decimal(str(value))


def _market_decimal(value: Any) -> Decimal:
    text = str(value or "0").strip()
    if text[:1] in {"C", "H"}:
        text = text[1:]
    return Decimal(text or "0")


def _ib_asset_class(row: dict[str, Any]) -> AssetClass:
    code = str(row.get("assetClass") or row.get("secType") or "STK").upper()
    return {
        "STK": AssetClass.EQUITY,
        "CASH": AssetClass.FX,
        "FX": AssetClass.FX,
        "FUT": AssetClass.FUTURE,
        "OPT": AssetClass.OPTION,
        "BOND": AssetClass.BOND,
        "FUND": AssetClass.FUND,
        "CRYPTO": AssetClass.CRYPTO,
    }.get(code, AssetClass.EQUITY)


@dataclass(frozen=True)
class MarketQuote:
    symbol: str
    last_price: Decimal
    bid: Decimal | None = None
    ask: Decimal | None = None
    raw: dict[str, Any] | None = None


class ReadOnlyJsonTransport:
    """HTTP transport deliberately exposing GET only; order writes cannot be expressed."""

    def __init__(self, base_url: str, headers: dict[str, str] | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = dict(headers or {})

    def get(self, path: str, params: dict[str, str] | None = None) -> Any:
        suffix = f"?{urlencode(params)}" if params else ""
        request = Request(
            f"{self.base_url}{path}{suffix}",
            headers=self.headers,
            method="GET",
        )
        with urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))


class _GhostLiveBrokerAdapter(BrokerAdapter):
    """Read live state, but irrevocably route executions to the local paper ledger."""

    broker_name = "live-broker"

    def __init__(self, paper_broker: PaperBrokerService) -> None:
        if _env_requests_live_money() or LIVE_ORDER_NETWORK_REQUESTS_ENABLED:
            raise LiveTradingDisabled(
                "Live-money activation is forbidden in this build; set TRADING_LIVE_MONEY_ACTIVE=false"
            )
        self.paper_broker = paper_broker

    def submit_order(self, order: OrderIntent) -> ExecutionResult:
        logger.warning(
            "%s live order blocked; routing %s %s x%s to paper ledger",
            self.broker_name,
            order.side.value,
            order.symbol,
            order.quantity,
        )
        return self.paper_broker.submit_order(order)

    def submit(self, order: OrderIntent) -> ExecutionResult:
        return self.submit_order(order)

    def buy(self, order: OrderIntent) -> ExecutionResult:
        if order.side != Side.BUY:
            raise ValueError("buy requires BUY side")
        return self.submit_order(order)

    def sell(self, order: OrderIntent) -> ExecutionResult:
        if order.side != Side.SELL:
            raise ValueError("sell requires SELL side")
        return self.submit_order(order)

    def cancel_order(self, order_id: str, tenant_id: str = "default") -> bool:
        logger.warning(
            "%s live cancel blocked; applying cancellation only to paper ledger order %s",
            self.broker_name,
            order_id,
        )
        return self.paper_broker.cancel_order(order_id, tenant_id)

    def cancel(self, order_id: str, tenant_id: str = "default") -> bool:
        return self.cancel_order(order_id, tenant_id)

    def get_margin(self, tenant_id: str = "default") -> BrokerMargin:
        summary = self.get_account_summary(tenant_id)
        return BrokerMargin(
            tenant_id=tenant_id,
            starting_capital=summary.net_liquidation,
            cash_balance=summary.cash_balance,
            gross_position_value=max(Decimal(0), summary.net_liquidation - summary.cash_balance),
            available_margin=summary.available_margin,
        )


class ZerodhaKiteAdapter(_GhostLiveBrokerAdapter):
    broker_name = "zerodha-kite"

    def __init__(
        self,
        api_key: str,
        access_token: str,
        paper_broker: PaperBrokerService,
        *,
        transport: ReadOnlyJsonTransport | None = None,
    ) -> None:
        super().__init__(paper_broker)
        self.transport = transport or ReadOnlyJsonTransport(
            "https://api.kite.trade",
            {
                "X-Kite-Version": "3",
                "Authorization": f"token {api_key}:{access_token}",
            },
        )

    def get_account_summary(self, tenant_id: str = "default") -> BrokerAccountSummary:
        payload = self.transport.get("/user/margins")
        equity = payload.get("data", payload).get("equity", {})
        available = equity.get("available", {})
        cash = _decimal(available.get("cash"))
        net = _decimal(equity.get("net"), str(cash))
        opening = _decimal(available.get("opening_balance"), str(cash))
        return BrokerAccountSummary(
            tenant_id=tenant_id,
            currency="INR",
            cash_balance=cash,
            net_liquidation=net if net else opening,
            available_margin=_decimal(available.get("live_balance"), str(cash)),
            raw=equity,
        )

    def get_positions(self, tenant_id: str = "default") -> tuple[BrokerPosition, ...]:
        payload = self.transport.get("/portfolio/positions")
        rows = payload.get("data", payload).get("net", [])
        positions = []
        for row in rows:
            quantity = int(row.get("quantity", 0))
            if quantity == 0:
                continue
            positions.append(
                BrokerPosition(
                    tenant_id=tenant_id,
                    symbol=str(row.get("tradingsymbol", "")),
                    market=Market.INDIA,
                    asset_class=(
                        AssetClass.COMMODITY
                        if str(row.get("exchange", "")).upper() == "MCX"
                        else AssetClass.EQUITY
                    ),
                    quantity=quantity,
                    average_price=_decimal(row.get("average_price")),
                )
            )
        return tuple(positions)

    def get_market_data_quote(self, instrument: str) -> MarketQuote:
        payload = self.transport.get("/quote", {"i": instrument})
        row = payload.get("data", payload).get(instrument, {})
        depth = row.get("depth", {})
        buy = depth.get("buy", [])
        sell = depth.get("sell", [])
        return MarketQuote(
            symbol=instrument,
            last_price=_decimal(row.get("last_price")),
            bid=_decimal(buy[0].get("price")) if buy else None,
            ask=_decimal(sell[0].get("price")) if sell else None,
            raw=row,
        )


class InteractiveBrokersAdapter(_GhostLiveBrokerAdapter):
    broker_name = "interactive-brokers"

    def __init__(
        self,
        account_id: str,
        paper_broker: PaperBrokerService,
        *,
        base_url: str = "https://localhost:5000/v1/api",
        transport: ReadOnlyJsonTransport | None = None,
    ) -> None:
        super().__init__(paper_broker)
        self.account_id = account_id
        self.transport = transport or ReadOnlyJsonTransport(base_url)
        self._portfolio_ready = False
        self._marketdata_ready = False

    def _ensure_portfolio_ready(self) -> None:
        if not self._portfolio_ready:
            self.transport.get("/portfolio/accounts")
            self._portfolio_ready = True

    def _ensure_marketdata_ready(self) -> None:
        if not self._marketdata_ready:
            self.transport.get("/iserver/accounts")
            self._marketdata_ready = True

    def get_account_summary(self, tenant_id: str = "default") -> BrokerAccountSummary:
        self._ensure_portfolio_ready()
        payload = self.transport.get(f"/portfolio/{self.account_id}/summary")

        def value(key: str) -> Any:
            item = payload.get(key, {})
            return item.get("amount", item) if isinstance(item, dict) else item

        currency_item = payload.get("currency", {})
        currency = (
            str(currency_item.get("currency", "USD"))
            if isinstance(currency_item, dict)
            else str(currency_item or "USD")
        )
        return BrokerAccountSummary(
            tenant_id=tenant_id,
            currency=currency,
            cash_balance=_decimal(value("totalcashvalue")),
            net_liquidation=_decimal(value("netliquidation")),
            available_margin=_decimal(value("availablefunds")),
            raw=payload,
        )

    def get_positions(self, tenant_id: str = "default") -> tuple[BrokerPosition, ...]:
        self._ensure_portfolio_ready()
        payload = self.transport.get(f"/portfolio/{self.account_id}/positions/0")
        rows = payload if isinstance(payload, list) else payload.get("positions", [])
        positions = []
        for row in rows:
            quantity = int(_decimal(row.get("position")))
            if quantity == 0:
                continue
            positions.append(
                BrokerPosition(
                    tenant_id=tenant_id,
                    symbol=str(row.get("ticker") or row.get("contractDesc") or row.get("conid", "")),
                    market=Market.GLOBAL,
                    asset_class=_ib_asset_class(row),
                    quantity=quantity,
                    average_price=_decimal(row.get("avgCost")),
                )
            )
        return tuple(positions)

    def get_market_data_quote(self, conid: str) -> MarketQuote:
        self._ensure_marketdata_ready()
        payload = self.transport.get(
            "/iserver/marketdata/snapshot",
            {"conids": conid, "fields": "31,84,86"},
        )
        row = payload[0] if isinstance(payload, list) and payload else {}
        return MarketQuote(
            symbol=conid,
            last_price=_market_decimal(row.get("31")),
            bid=_market_decimal(row.get("84")) if row.get("84") not in (None, "") else None,
            ask=_market_decimal(row.get("86")) if row.get("86") not in (None, "") else None,
            raw=row,
        )
