from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from quant_ai.brokers.adapter import BrokerAdapter, BrokerMargin, BrokerPosition
from quant_ai.brokers.base import ExecutionResult
from quant_ai.domain.models import OrderIntent, Side
from quant_ai.execution.broker import BrokerAccountSummary
from quant_ai.execution.broker_reads import (
    BrokerReadError,
    ExternalBrokerFunds,
    ExternalBrokerPosition,
    amount,
    ib_funds,
    ib_position,
    identifier,
    integer,
    kite_data,
    kite_funds,
    kite_positions,
)
from quant_ai.execution.live_guard import LiveTradingDisabled
from quant_ai.execution.paper_ledger import PaperBrokerService

logger = logging.getLogger(__name__)

# Compile-time firewall: this release has no live-order network path.
LIVE_ORDER_NETWORK_REQUESTS_ENABLED = False


def _env_requests_live_money() -> bool:
    return os.getenv("TRADING_LIVE_MONEY_ACTIVE", "false").strip().lower() == "true"


def _quote_amount(value: Any) -> Decimal:
    text = str(value).strip()
    if text[:1] in {"C", "H"}:
        text = text[1:]
    result = amount(text, "quote price")
    if result <= 0:
        raise BrokerReadError("Broker quote price is not positive")
    return result


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BrokerReadError("Broker read redirected; credentials were not forwarded")


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
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise BrokerReadError("Broker base URL must be HTTPS without credentials, query or fragment")
        self.base_url = base_url.rstrip("/")
        self.headers = dict(headers or {})

    def get(self, path: str, params: dict[str, str] | None = None) -> Any:
        if not path.startswith("/") or path.startswith("//") or ".." in path or "?" in path or "#" in path or "\\" in path or any(ord(c) < 32 for c in path):
            raise BrokerReadError("Invalid broker read path")
        suffix = f"?{urlencode(params)}" if params else ""
        request = Request(
            f"{self.base_url}{path}{suffix}",
            headers=self.headers,
            method="GET",
        )
        with build_opener(_NoRedirect()).open(request, timeout=10) as response:
            body = response.read(8_000_001)
            if len(body) > 8_000_000:
                raise BrokerReadError("Broker response exceeds 8 MB")
            def pairs(items):
                result = {}
                for key, value in items:
                    if key in result:
                        raise BrokerReadError("Duplicate broker JSON key")
                    result[key] = value
                return result
            def invalid_constant(value):
                raise BrokerReadError("Nonfinite broker JSON value")
            return json.loads(body.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid_constant, parse_float=Decimal)


class _GhostLiveBrokerAdapter(BrokerAdapter):
    """Paper execution and account state; external reads have an explicit separate API."""

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
        return self.paper_broker.get_margin(tenant_id)

    def get_positions(self, tenant_id: str = "default") -> tuple[BrokerPosition, ...]:
        return self.paper_broker.get_positions(tenant_id)

    def get_account_summary(self, tenant_id: str = "default") -> BrokerAccountSummary:
        return self.paper_broker.get_account_summary(tenant_id)


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

    def external_account_id(self, expected_account_id: str) -> str:
        expected = identifier(expected_account_id, "expected account ID")
        data = kite_data(self.transport.get("/user/profile"))
        if not isinstance(data, dict) or data.get("user_id") != expected:
            raise BrokerReadError("Kite account identity does not match the selected external account")
        return expected

    def read_external_funds(self, expected_account_id: str) -> ExternalBrokerFunds:
        account_id = self.external_account_id(expected_account_id)
        funds = kite_funds(self.transport.get("/user/margins"), account_id)
        self.external_account_id(expected_account_id)
        return funds

    def read_external_positions(self, expected_account_id: str) -> tuple[ExternalBrokerPosition, ...]:
        account_id = self.external_account_id(expected_account_id)
        positions = kite_positions(self.transport.get("/portfolio/positions"), account_id)
        self.external_account_id(expected_account_id)
        return positions

    def get_market_data_quote(self, instrument: str) -> MarketQuote:
        payload = self.transport.get("/quote", {"i": instrument})
        data = kite_data(payload)
        row = data.get(instrument) if isinstance(data, dict) else None
        if not isinstance(row, dict):
            raise BrokerReadError("Kite quote is missing")
        depth = row.get("depth", {})
        buy = depth.get("buy", [])
        sell = depth.get("sell", [])
        return MarketQuote(
            symbol=instrument,
            last_price=_quote_amount(row.get("last_price")),
            bid=_quote_amount(buy[0].get("price")) if buy else None,
            ask=_quote_amount(sell[0].get("price")) if sell else None,
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
        self.account_id = identifier(account_id, "account ID")
        if not account_id.isascii() or not account_id.isalnum():
            raise BrokerReadError("IBKR account ID must be ASCII alphanumeric")
        self.transport = transport or ReadOnlyJsonTransport(base_url)

    def _ensure_portfolio_ready(self) -> None:
        accounts = self.transport.get("/portfolio/accounts")
        if not isinstance(accounts, list) or not any(isinstance(row, dict) and row.get("id") == self.account_id for row in accounts):
            raise BrokerReadError("Selected IBKR account is not in the portfolio account response")

    def _ensure_marketdata_ready(self) -> None:
        payload = self.transport.get("/iserver/accounts")
        if not isinstance(payload, dict) or not isinstance(payload.get("accounts"), list) or self.account_id not in payload["accounts"]:
            raise BrokerReadError("Selected IBKR account is not in the brokerage account response")

    def read_external_funds(self) -> ExternalBrokerFunds:
        self._ensure_portfolio_ready()
        return ib_funds(self.transport.get(f"/portfolio/{self.account_id}/summary"), self.account_id)

    def read_external_positions(self) -> tuple[ExternalBrokerPosition, ...]:
        self._ensure_portfolio_ready()
        positions, seen = [], set()
        for page in range(101):
            rows = self.transport.get(f"/portfolio/{self.account_id}/positions/{page}")
            if not isinstance(rows, list) or len(rows) > 100:
                raise BrokerReadError("Invalid IBKR position page")
            if not rows:
                return tuple(positions)
            if page == 100:
                raise BrokerReadError("IBKR positions exceed the 100-page capture limit")
            for row in rows:
                position = ib_position(row, self.account_id)
                key = (position.instrument_id, position.model)
                if key in seen:
                    raise BrokerReadError("Duplicate IBKR contract/model or repeated position page")
                seen.add(key)
                if position.quantity:
                    positions.append(position)
        raise BrokerReadError("IBKR position pagination did not finish")

    def get_market_data_quote(self, conid: str) -> MarketQuote:
        conid = str(integer(conid, "contract ID", positive=True))
        self._ensure_marketdata_ready()
        payload = self.transport.get(
            "/iserver/marketdata/snapshot",
            {"conids": conid, "fields": "31,84,86"},
        )
        if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict) or str(payload[0].get("conid")) != conid:
            raise BrokerReadError("IBKR quote identity is missing or different")
        row = payload[0]
        return MarketQuote(
            symbol=conid,
            last_price=_quote_amount(row.get("31")),
            bid=_quote_amount(row.get("84")) if row.get("84") not in (None, "") else None,
            ask=_quote_amount(row.get("86")) if row.get("86") not in (None, "") else None,
            raw=row,
        )
