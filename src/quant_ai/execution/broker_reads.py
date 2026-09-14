"""Explicit external observations, never an execution account or inferred equity.

Broker quantity, contract, product and currency identity survive normalization.
These records are deliberately not BrokerPosition/BrokerMargin: a paper execution
gateway must not use a different account's balances or holdings for its decisions.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


class BrokerReadError(ValueError):
    """A read failed to establish the requested external state."""


def identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 160 or any(ord(c) < 32 for c in value):
        raise BrokerReadError(f"Invalid broker {field}")
    return value


def amount(value: Any, field: str, *, optional: bool = False) -> Decimal | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise BrokerReadError(f"Missing or invalid broker {field}")
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise BrokerReadError(f"Invalid broker {field}") from error
    if not result.is_finite():
        raise BrokerReadError(f"Nonfinite broker {field}")
    return result


def integer(value: Any, field: str, *, positive: bool = False) -> int:
    result = amount(value, field)
    if result != result.to_integral_value() or abs(result) > 2**53 - 1 or positive and result <= 0:
        raise BrokerReadError(f"Invalid broker integer {field}")
    return int(result)


@dataclass(frozen=True)
class ExternalBrokerFunds:
    broker: str
    account_id: str
    currency: str | None
    cash_balance: Decimal | None
    available_balance: Decimal | None
    net_liquidation: Decimal | None
    # Kite's `net` is available trading funds, not portfolio liquidation value.
    net_trading_funds: Decimal | None = None
    opening_balance: Decimal | None = None
    segment: str | None = None


@dataclass(frozen=True)
class ExternalBrokerPosition:
    broker: str
    account_id: str
    instrument_id: str
    symbol: str
    exchange: str | None
    currency: str | None
    security_type: str | None
    product: str | None
    quantity: Decimal
    average_cost: Decimal
    # A broker position row is not a depository-holdings inventory or tradability approval.
    scope: str
    cost_basis: str = "per_unit"
    model: str | None = None


def kite_data(payload: Any) -> Any:
    if not isinstance(payload, dict) or payload.get("status") != "success" or "data" not in payload:
        raise BrokerReadError("Kite did not return a successful data envelope")
    return payload["data"]


def kite_funds(payload: Any, account_id: str) -> ExternalBrokerFunds:
    data = kite_data(payload)
    equity = data.get("equity") if isinstance(data, dict) else None
    if not isinstance(equity, dict) or type(equity.get("enabled")) is not bool:
        raise BrokerReadError("Kite equity margin segment is missing or invalid")
    if not equity["enabled"]:
        raise BrokerReadError("Kite equity margin segment is disabled")
    available = equity.get("available")
    if not isinstance(available, dict):
        raise BrokerReadError("Kite available-funds evidence is missing")
    return ExternalBrokerFunds(
        "zerodha-kite", account_id, "INR", amount(available.get("cash"), "cash", optional=True),
        amount(available.get("live_balance"), "live balance", optional=True), None,
        net_trading_funds=amount(equity.get("net"), "net trading funds", optional=True),
        opening_balance=amount(available.get("opening_balance"), "opening balance", optional=True), segment="equity",
    )


def kite_positions(payload: Any, account_id: str) -> tuple[ExternalBrokerPosition, ...]:
    data = kite_data(payload)
    rows = data.get("net") if isinstance(data, dict) else None
    if not isinstance(rows, list) or len(rows) > 10000:
        raise BrokerReadError("Kite net-position collection is missing or exceeds 10000 rows")
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise BrokerReadError("Invalid Kite position row")
        token = str(integer(row.get("instrument_token"), "instrument token", positive=True))
        symbol = identifier(row.get("tradingsymbol"), "trading symbol")
        exchange = identifier(row.get("exchange"), "exchange")
        product = identifier(row.get("product"), "product")
        key = (token, exchange, product)
        if key in seen:
            raise BrokerReadError("Duplicate Kite position identity")
        seen.add(key)
        quantity = integer(row.get("quantity"), "quantity")
        price = amount(row.get("average_price"), "average price")
        if quantity:
            result.append(ExternalBrokerPosition("zerodha-kite", account_id, token, symbol, exchange,
                None, None, product, Decimal(quantity), price, "broker_net_positions_excludes_separate_holdings"))
    return tuple(result)


def ib_position(row: Any, account_id: str) -> ExternalBrokerPosition:
    if not isinstance(row, dict) or row.get("acctId") != account_id:
        raise BrokerReadError("IBKR position account identity is missing or different")
    conid = str(integer(row.get("conid"), "contract ID", positive=True))
    quantity = amount(row.get("position"), "position")
    return ExternalBrokerPosition("interactive-brokers", account_id, conid,
        identifier(row.get("ticker") or row.get("contractDesc"), "contract description"),
        identifier(row["listingExchange"], "listing exchange") if row.get("listingExchange") else None,
        identifier(row["currency"], "currency") if row.get("currency") else None,
        identifier(row["assetClass"], "security type") if row.get("assetClass") else None,
        None, quantity, amount(row.get("avgCost"), "average cost"), "broker_contract_positions_cached_paginated",
        "per_contract_including_multiplier", identifier(row["model"], "model") if row.get("model") else None)


def ib_funds(payload: Any, account_id: str) -> ExternalBrokerFunds:
    if not isinstance(payload, dict) or not isinstance(payload.get("accountcode"), dict) or payload["accountcode"].get("value") != account_id:
        raise BrokerReadError("IBKR summary account identity is missing or different")
    currencies, values = set(), []
    for key in ("totalcashvalue", "availablefunds", "netliquidation"):
        row = payload.get(key)
        if row is None:
            values.append(None)
            continue
        if not isinstance(row, dict):
            raise BrokerReadError("IBKR summary amount evidence is invalid")
        if row.get("isNull") is True or row.get("isNone") is True:
            values.append(None)
            continue
        currency = identifier(row.get("currency"), "summary currency")
        if len(currency) != 3 or not currency.isascii() or not currency.isupper() or not currency.isalpha():
            raise BrokerReadError("IBKR summary lacks an explicit currency code")
        currencies.add(currency)
        values.append(amount(row.get("amount"), key))
    if len(currencies) > 1:
        raise BrokerReadError("IBKR summary mixes currencies")
    return ExternalBrokerFunds("interactive-brokers", account_id, next(iter(currencies), None), *values)
