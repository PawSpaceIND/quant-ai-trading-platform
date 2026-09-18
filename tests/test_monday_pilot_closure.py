from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from quant_ai.domain.models import AssetClass, Instrument, Market, OrderIntent, Side
from quant_ai.execution.daemon import AutonomousTradingDaemon
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.execution.protective_exits import ExitTrigger, ProtectiveExitEngine
from quant_ai.execution.session import MarketCalendar
from quant_ai.marketdata.live_feed import LiveTickMarketDataFeed
from quant_ai.marketdata.models import Candle
from quant_ai.marketdata.ticker_stream import TickBuffer


def test_session_flatten_is_deterministic_risk_reducing_exit(tmp_path):
    broker = PaperBrokerService(tmp_path / "paper.db")
    broker.buy(
        OrderIntent(
            "INFY", Market.INDIA, Side.BUY, 2, D(100), "test",
            asset_class=AssetClass.EQUITY, tenant_id="pilot",
            stop_price=D(95), take_profit_price=D(110),
        )
    )
    now = datetime(2026, 9, 21, 9, 55, tzinfo=timezone.utc)
    engine = ProtectiveExitEngine(broker, lambda _position: D(101), tenant_id="pilot")

    result = engine.flatten_session(now)

    assert len(result) == 1
    assert result[0].trigger is ExitTrigger.SESSION_FLATTEN
    assert result[0].filled is True
    assert broker.get_positions("pilot") == ()
    evidence = broker.protected_fill_receipt(result[0].order_id, "pilot").evidence
    assert evidence["trigger"] == "SESSION_FLATTEN"
    assert evidence["risk_verdict"]["approved"] == "true"


def test_daemon_flatten_window_is_exchange_close_aware():
    instrument = Instrument("INFY", Market.INDIA, AssetClass.EQUITY, "INR", "NSE")
    calls = []
    daemon = object.__new__(AutonomousTradingDaemon)
    daemon.session_flatten_minutes = 15
    daemon.instruments = (instrument,)
    daemon.scheduler = SimpleNamespace(calendar=MarketCalendar())
    daemon.exit_engine = SimpleNamespace(
        flatten_session=lambda now, symbols: calls.append((now, symbols)) or ()
    )

    ist = ZoneInfo("Asia/Kolkata")
    before = datetime(2026, 9, 21, 15, 14, tzinfo=ist)
    inside = datetime(2026, 9, 21, 15, 16, tzinfo=ist)

    assert daemon._flatten_session_positions(before) == ()
    assert calls == []
    assert daemon._flatten_session_positions(inside) == ()
    assert calls == [(inside, {"INFY"})]


def test_live_feed_can_start_with_prior_closed_minute_history():
    instrument = Instrument("INFY", Market.INDIA, AssetClass.EQUITY, "INR", "NSE")
    now = datetime(2026, 9, 21, 3, 50, tzinfo=timezone.utc)
    feed = LiveTickMarketDataFeed(TickBuffer(clock=lambda: now), clock=lambda: now)
    start = datetime(2026, 9, 18, 4, 0, tzinfo=timezone.utc)
    candles = tuple(
        Candle(
            instrument,
            start + timedelta(minutes=index + 1),
            D(100), D(101), D(99), D(100) + D(index) / D(100), D(1000),
        )
        for index in range(60)
    )

    feed.seed_closed_candles(candles, now)
    recent = feed.fetch_recent_ohlcv(instrument, now, count=60)

    assert len(recent) == 60
    assert recent[0].timestamp == candles[0].timestamp
    assert recent[-1].timestamp == candles[-1].timestamp
