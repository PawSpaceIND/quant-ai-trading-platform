from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant_ai.domain.models import AssetClass, Instrument, Market, OrderIntent, Side
from quant_ai.execution.live_factory import LiveBrokerFactory, LiveMoneyDisabledError
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.marketdata.feed import IndiaSandboxMarketDataFeed, UsaSandboxMarketDataFeed


def test_paper_broker_service_persists_cash_positions_and_ledger(tmp_path) -> None:
    broker = PaperBrokerService(tmp_path / "paper.db", starting_capital=Decimal(1000), slippage_bps=Decimal(0))
    buy = OrderIntent("AAPL", Market.USA, Side.BUY, 4, Decimal(100), "test", tenant_id="t1")
    fill = broker.buy(buy)
    assert fill.status == "FILLED"
    assert broker.get_margin("t1").cash_balance == Decimal(600)
    assert broker.get_positions("t1")[0].quantity == 4
    sell = OrderIntent("AAPL", Market.USA, Side.SELL, 1, Decimal(110), "test", tenant_id="t1")
    broker.sell(sell)
    assert broker.get_margin("t1").cash_balance == Decimal(710)
    assert broker.get_positions("t1")[0].quantity == 3
    assert [entry.side for entry in broker.ledger_entries("t1")] == [Side.BUY, Side.SELL]


def test_paper_broker_rejects_overspend_and_naked_sell() -> None:
    broker = PaperBrokerService(starting_capital=Decimal(100), slippage_bps=Decimal(0))
    with pytest.raises(ValueError, match="insufficient_paper_cash"):
        broker.submit(OrderIntent("AAPL", Market.USA, Side.BUY, 2, Decimal(100), "test"))
    with pytest.raises(ValueError, match="insufficient_paper_position"):
        broker.submit(OrderIntent("AAPL", Market.USA, Side.SELL, 1, Decimal(100), "test"))


def test_cancel_is_fail_closed_for_immediate_fills() -> None:
    broker = PaperBrokerService(slippage_bps=Decimal(0))
    fill = broker.submit(OrderIntent("AAPL", Market.USA, Side.BUY, 1, Decimal(100), "test"))
    assert broker.cancel(fill.order_id) is False
    assert broker.cancel("missing") is False


def test_live_factory_never_constructs_live_broker(monkeypatch) -> None:
    monkeypatch.delenv("TRADING_LIVE_MONEY_ACTIVE", raising=False)
    with pytest.raises(LiveMoneyDisabledError, match="not explicitly true"):
        LiveBrokerFactory.create()
    monkeypatch.setenv("TRADING_LIVE_MONEY_ACTIVE", "true")
    with pytest.raises(LiveMoneyDisabledError, match="intentionally disabled"):
        LiveBrokerFactory.create()


def test_india_and_us_sandbox_market_data_are_structured() -> None:
    start = datetime(2026, 1, 2, 9, 15, tzinfo=timezone.utc)
    end = start + timedelta(minutes=3)
    india_instrument = Instrument("RELIANCE", Market.INDIA, AssetClass.EQUITY, "INR", "NSE")
    us_instrument = Instrument("AAPL", Market.USA, AssetClass.EQUITY, "USD", "NASDAQ")
    india = IndiaSandboxMarketDataFeed()
    usa = UsaSandboxMarketDataFeed()
    assert len(india.fetch_ohlcv(india_instrument, start, end)) == 3
    tick = usa.latest_tick(us_instrument)
    assert tick.bid < tick.ask
    assert tick.instrument.symbol == "AAPL"
    with pytest.raises(ValueError, match="instrument_market_mismatch"):
        india.latest_tick(us_instrument)
