from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from quant_ai.domain.models import Market, OrderIntent, Side
from quant_ai.execution.live_brokers import InteractiveBrokersAdapter, ZerodhaKiteAdapter
from quant_ai.execution.live_guard import LiveTradingDisabled
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.execution.session import GlobalVenue, MarketCalendar, MarketState


class FakeReadOnlyTransport:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, params))
        return self.responses[path]


def _buy(symbol: str, market: Market, tenant_id: str = "ghost") -> OrderIntent:
    return OrderIntent(
        symbol,
        market,
        Side.BUY,
        2,
        Decimal(100),
        "ghost-test",
        tenant_id=tenant_id,
    )


def test_zerodha_submit_is_intercepted_before_any_network_request(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("TRADING_LIVE_MONEY_ACTIVE", "false")
    transport = FakeReadOnlyTransport()
    paper = PaperBrokerService(tmp_path / "zerodha.db", starting_capital=Decimal(10000))
    adapter = ZerodhaKiteAdapter("key", "token", paper, transport=transport)

    fill = adapter.submit_order(_buy("RELIANCE", Market.INDIA))

    assert fill.status == "FILLED"
    assert fill.order_id.startswith("PAPER-")
    assert transport.calls == []
    assert len(paper.ledger_entries("ghost")) == 1
    assert "live order blocked" in caplog.text


def test_ib_submit_is_intercepted_before_any_network_request(tmp_path, monkeypatch):
    monkeypatch.delenv("TRADING_LIVE_MONEY_ACTIVE", raising=False)
    transport = FakeReadOnlyTransport()
    paper = PaperBrokerService(tmp_path / "ib.db", starting_capital=Decimal(10000))
    adapter = InteractiveBrokersAdapter("DU123", paper, transport=transport)

    fill = adapter.submit_order(_buy("AAPL", Market.USA, tenant_id="ib-ghost"))

    assert fill.status == "FILLED"
    assert fill.order_id.startswith("PAPER-")
    assert transport.calls == []
    assert len(paper.ledger_entries("ib-ghost")) == 1


def test_adapters_fail_closed_if_live_money_env_is_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_LIVE_MONEY_ACTIVE", "true")
    paper = PaperBrokerService(tmp_path / "blocked.db")

    with pytest.raises(LiveTradingDisabled, match="forbidden"):
        ZerodhaKiteAdapter("key", "token", paper, transport=FakeReadOnlyTransport())
    with pytest.raises(LiveTradingDisabled, match="forbidden"):
        InteractiveBrokersAdapter("DU123", paper, transport=FakeReadOnlyTransport())


def test_zerodha_read_only_account_positions_and_quote(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_LIVE_MONEY_ACTIVE", "false")
    transport = FakeReadOnlyTransport(
        {
            "/user/profile": {"status":"success","data":{"user_id":"AB1234"}},
            "/user/margins": {
                "status":"success", "data": {
                    "equity": {
                        "net": 120000, "enabled": True,
                        "available": {"cash": 90000, "live_balance": 88000},
                    }
                }
            },
            "/portfolio/positions": {
                "status":"success", "data": {
                    "net": [
                        {"tradingsymbol": "RELIANCE", "quantity": 3, "average_price": 2500, "instrument_token": 738561, "exchange":"NSE", "product":"CNC"},
                    ]
                }
            },
            "/quote": {
                "status":"success", "data": {
                    "NSE:RELIANCE": {
                        "last_price": 2550,
                        "depth": {"buy": [{"price": 2549}], "sell": [{"price": 2551}]},
                    }
                }
            },
        }
    )
    paper = PaperBrokerService(tmp_path / "zerodha-reads.db")
    adapter = ZerodhaKiteAdapter("key", "token", paper, transport=transport)

    summary = adapter.read_external_funds("AB1234")
    positions = adapter.read_external_positions("AB1234")
    quote = adapter.get_market_data_quote("NSE:RELIANCE")

    assert summary.cash_balance == Decimal(90000)
    assert summary.net_liquidation is None
    assert summary.net_trading_funds == Decimal(120000)
    assert positions[0].symbol == "RELIANCE"
    assert quote.last_price == Decimal(2550)
    assert all(call[0] in {"/user/profile", "/user/margins", "/portfolio/positions", "/quote"} for call in transport.calls)


def test_ib_read_only_account_positions_and_quote(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_LIVE_MONEY_ACTIVE", "false")
    transport = FakeReadOnlyTransport(
        {
            "/portfolio/accounts": [{"id": "DU123"}],
            "/iserver/accounts": {"accounts": ["DU123"]},
            "/portfolio/DU123/summary": {
                "accountcode": {"value": "DU123"},
                "totalcashvalue": {"amount": 50000, "currency":"USD"},
                "netliquidation": {"amount": 75000, "currency":"USD"},
                "availablefunds": {"amount": 45000, "currency":"USD"},
                "currency": {"currency": "USD"},
            },
            "/portfolio/DU123/positions/0": [
                {"acctId":"DU123", "conid":272093, "ticker": "MSFT", "position": 4.25, "avgCost": 420, "assetClass": "STK", "currency":"USD"},
            ],
            "/portfolio/DU123/positions/1": [],
            "/iserver/marketdata/snapshot": [
                {"conid":272093, "31": "425", "84": "424.5", "86": "425.5"},
            ],
        }
    )
    paper = PaperBrokerService(tmp_path / "ib-reads.db")
    adapter = InteractiveBrokersAdapter("DU123", paper, transport=transport)

    summary = adapter.read_external_funds()
    positions = adapter.read_external_positions()
    quote = adapter.get_market_data_quote("272093")

    assert summary.currency == "USD"
    assert summary.net_liquidation == Decimal(75000)
    assert positions[0].symbol == "MSFT"
    assert positions[0].quantity == Decimal("4.25")
    assert quote.bid == Decimal("424.5")


def test_global_market_calendar_reports_overlapping_venue_states():
    calendar = MarketCalendar()
    now = datetime(2026, 9, 14, 8, 30, tzinfo=timezone.utc)

    states = calendar.global_states(now)

    assert states[GlobalVenue.LONDON] == MarketState.REGULAR_HOURS
    assert states[GlobalVenue.FRANKFURT] == MarketState.REGULAR_HOURS
    assert states[GlobalVenue.USA] == MarketState.PRE_MARKET
    assert states[GlobalVenue.INDIA] == MarketState.REGULAR_HOURS
    assert states[GlobalVenue.TOKYO] == MarketState.CLOSED
