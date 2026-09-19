from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from quant_ai.domain.models import AssetClass, Instrument, Market, OrderIntent, Side
from quant_ai.execution.briefing import FounderExecutionBrief
from quant_ai.execution.daemon import AutonomousTradingDaemon
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.execution.protective_exits import ExitTrigger, ProtectiveExitEngine
from quant_ai.execution.session import MarketCalendar, MarketState
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


def test_runtime_certification_reports_safe_operational_state(monkeypatch):
    import importlib.util
    from pathlib import Path

    script = Path(__file__).parents[1] / "scripts/india_paper_runtime.py"
    spec = importlib.util.spec_from_file_location("india_runtime_certification_test", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("TRADING_LIVE_MONEY_ACTIVE", "false")
    monkeypatch.setenv("PRAMANA_RELEASE_REVISION", "abc123")
    monkeypatch.setenv("PRAMANA_ZERODHA_SYMBOLS_JSON", '{"1":"INFY","2":"TCS","3":"RELIANCE","4":"GOLDBEES","5":"SILVERBEES"}')
    monkeypatch.setenv("PRAMANA_SESSION_FLATTEN_MINUTES", "15")
    monkeypatch.setenv("PRAMANA_DAILY_HISTORY_PROVIDER", "kite")
    monkeypatch.setenv("PRAMANA_REQUIRE_BOOK_RISK_GATES", "true")
    monkeypatch.setenv("PRAMANA_OVERNIGHT_GROSS_CAP", "0.25")
    monkeypatch.setenv("PRAMANA_OVERNIGHT_CLOSING_WINDOW_MINUTES", "15")
    broker = SimpleNamespace(
        reconcile=lambda tenant: {"status": "matched"},
        protection_coverage=lambda tenant: {"status": "complete"},
        get_starting_capital=lambda tenant: D(100000),
    )
    briefs = tuple(
        FounderExecutionBrief(
            datetime(2026, 9, 21, 4, 0, tzinfo=timezone.utc),
            MarketState.REGULAR_HOURS, symbol, "PRESERVE_CAPITAL", (), "test", (),
            ("price=FRESH", "news=FRESH", "macro=FRESH", "fundamentals=FRESH"),
        )
        for _, symbol in module.load_directives()[1]
    )
    traces = tuple(
        SimpleNamespace(
            subject=symbol,
            generated_at=datetime(2026, 9, 21, 4, 0, tzinfo=timezone.utc),
            provenance={"mode": "llm"},
        )
        for _, symbol in module.load_directives()[1]
    )
    logger = SimpleNamespace(traces=lambda: traces)
    runner = SimpleNamespace(
        daemon=SimpleNamespace(
            tracker=SimpleNamespace(broker=broker),
            briefs=briefs,
            scheduler=SimpleNamespace(
                pipeline=SimpleNamespace(runtime=SimpleNamespace(xai_logger=logger))
            ),
        ),
        intraday_warmup_status={symbol: {"status": "ready", "bars": 60, "updatedAt": "x"}
                                for _, symbol in module.load_directives()[1]},
    )

    report = module.runtime_certification(runner)

    assert report["revision"] == "abc123"
    assert report["ready"] is True
    assert report["reasons"] == []
    assert report["paperOnly"] is True
    assert report["startingCapital"] == "100000"
    assert report["watchlist"] == [symbol for _, symbol in module.load_directives()[1]]
    assert report["mappedSymbols"] == sorted(symbol for _, symbol in module.load_directives()[1])
    assert report["watchlistInstruments"] == [
        {"exchange": exchange, "symbol": symbol}
        for exchange, symbol in module.load_directives()[1]
    ]
    assert report["sessionFlattenMinutes"] == 15
    assert report["dailyHistoryProvider"] == "kite"
    assert report["bookRiskRequired"] is True
    assert report["overnightGrossCap"] == "0.25"
    assert report["overnightClosingWindowMinutes"] == "15"
    assert report["cadenceSubjects"] == sorted(symbol for _, symbol in module.load_directives()[1])
    assert all("price=FRESH" in report["cadenceProviderStatus"][symbol]
               for _, symbol in module.load_directives()[1])
    assert report["inferenceModeBySymbol"] == {
        symbol: "llm" for _, symbol in module.load_directives()[1]
    }
    assert report["reconciliation"] == "matched"
    assert report["protection"] == "complete"
    assert all(item["status"] == "ready" for item in report["intradayWarmup"].values())
    assert "ZERODHA_ACCESS_TOKEN" not in report
    assert "ANTHROPIC_API_KEY" not in report


def test_runtime_certification_refuses_incomplete_warmup(monkeypatch):
    import importlib.util
    from pathlib import Path

    script = Path(__file__).parents[1] / "scripts/india_paper_runtime.py"
    spec = importlib.util.spec_from_file_location("india_runtime_certification_negative", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("TRADING_LIVE_MONEY_ACTIVE", "false")
    monkeypatch.setenv("PRAMANA_RELEASE_REVISION", "abc123")
    monkeypatch.setenv("PRAMANA_ZERODHA_SYMBOLS_JSON", '{"1":"INFY","2":"TCS","3":"RELIANCE","4":"GOLDBEES","5":"SILVERBEES"}')
    monkeypatch.setenv("PRAMANA_SESSION_FLATTEN_MINUTES", "15")
    monkeypatch.setenv("PRAMANA_DAILY_HISTORY_PROVIDER", "kite")
    monkeypatch.setenv("PRAMANA_REQUIRE_BOOK_RISK_GATES", "true")
    monkeypatch.setenv("PRAMANA_OVERNIGHT_GROSS_CAP", "0.25")
    monkeypatch.setenv("PRAMANA_OVERNIGHT_CLOSING_WINDOW_MINUTES", "15")
    broker = SimpleNamespace(
        reconcile=lambda tenant: {"status": "matched"},
        protection_coverage=lambda tenant: {"status": "complete"},
        get_starting_capital=lambda tenant: D(100000),
    )
    warmup = {symbol: {"status": "ready", "bars": 60, "updatedAt": "x"}
              for _, symbol in module.load_directives()[1]}
    warmup["INFY"] = {"status": "insufficient", "bars": 49, "updatedAt": "x"}
    briefs = tuple(
        FounderExecutionBrief(
            datetime(2026, 9, 21, 4, 0, tzinfo=timezone.utc),
            MarketState.REGULAR_HOURS, symbol, "PRESERVE_CAPITAL", (), "test", (),
            ("price=FRESH",),
        )
        for _, symbol in module.load_directives()[1]
    )
    traces = tuple(
        SimpleNamespace(
            subject=symbol,
            generated_at=datetime(2026, 9, 21, 4, 0, tzinfo=timezone.utc),
            provenance={"mode": "llm"},
        )
        for _, symbol in module.load_directives()[1]
    )
    logger = SimpleNamespace(traces=lambda: traces)
    runner = SimpleNamespace(
        daemon=SimpleNamespace(
            tracker=SimpleNamespace(broker=broker),
            briefs=briefs,
            scheduler=SimpleNamespace(
                pipeline=SimpleNamespace(runtime=SimpleNamespace(xai_logger=logger))
            ),
        ),
        intraday_warmup_status=warmup,
    )

    report = module.runtime_certification(runner)

    assert report["ready"] is False
    assert report["reasons"] == ["intraday_warmup_not_ready"]


def test_runtime_certification_requires_all_five_fresh_price_observations(monkeypatch):
    import importlib.util
    from pathlib import Path

    script = Path(__file__).parents[1] / "scripts/india_paper_runtime.py"
    spec = importlib.util.spec_from_file_location("india_runtime_cadence_certification", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for key, value in {
        "TRADING_LIVE_MONEY_ACTIVE": "false",
        "PRAMANA_RELEASE_REVISION": "abc123",
        "PRAMANA_ZERODHA_SYMBOLS_JSON": '{"1":"INFY","2":"TCS","3":"RELIANCE","4":"GOLDBEES","5":"SILVERBEES"}',
        "PRAMANA_SESSION_FLATTEN_MINUTES": "15",
        "PRAMANA_DAILY_HISTORY_PROVIDER": "kite",
        "PRAMANA_REQUIRE_BOOK_RISK_GATES": "true",
        "PRAMANA_OVERNIGHT_GROSS_CAP": "0.25",
        "PRAMANA_OVERNIGHT_CLOSING_WINDOW_MINUTES": "15",
    }.items():
        monkeypatch.setenv(key, value)
    broker = SimpleNamespace(
        reconcile=lambda tenant: {"status": "matched"},
        protection_coverage=lambda tenant: {"status": "complete"},
        get_starting_capital=lambda tenant: D(100000),
    )
    briefs = tuple(
        FounderExecutionBrief(
            datetime(2026, 9, 21, 4, 0, tzinfo=timezone.utc),
            MarketState.REGULAR_HOURS, symbol, "PRESERVE_CAPITAL", (), "test", (),
            (("price=MISSING",) if symbol == "SILVERBEES" else ("price=FRESH",)),
        )
        for _, symbol in module.load_directives()[1]
    )
    traces = tuple(
        SimpleNamespace(
            subject=symbol,
            generated_at=datetime(2026, 9, 21, 4, 0, tzinfo=timezone.utc),
            provenance={"mode": "llm"},
        )
        for _, symbol in module.load_directives()[1]
    )
    logger = SimpleNamespace(traces=lambda: traces)
    runner = SimpleNamespace(
        daemon=SimpleNamespace(
            tracker=SimpleNamespace(broker=broker),
            briefs=briefs,
            scheduler=SimpleNamespace(
                pipeline=SimpleNamespace(runtime=SimpleNamespace(xai_logger=logger))
            ),
        ),
        intraday_warmup_status={symbol: {"status": "ready", "bars": 60, "updatedAt": "x"}
                                for _, symbol in module.load_directives()[1]},
    )

    report = module.runtime_certification(runner)

    assert report["ready"] is False
    assert report["reasons"] == ["five_symbol_price_freshness_not_observed"]


def test_runtime_certification_requires_llm_inference_for_all_five(monkeypatch):
    import importlib.util
    from pathlib import Path

    script = Path(__file__).parents[1] / "scripts/india_paper_runtime.py"
    spec = importlib.util.spec_from_file_location("india_runtime_llm_certification", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for key, value in {
        "TRADING_LIVE_MONEY_ACTIVE": "false",
        "PRAMANA_RELEASE_REVISION": "abc123",
        "PRAMANA_ZERODHA_SYMBOLS_JSON": '{"1":"INFY","2":"TCS","3":"RELIANCE","4":"GOLDBEES","5":"SILVERBEES"}',
        "PRAMANA_SESSION_FLATTEN_MINUTES": "15",
        "PRAMANA_DAILY_HISTORY_PROVIDER": "kite",
        "PRAMANA_REQUIRE_BOOK_RISK_GATES": "true",
        "PRAMANA_OVERNIGHT_GROSS_CAP": "0.25",
        "PRAMANA_OVERNIGHT_CLOSING_WINDOW_MINUTES": "15",
    }.items():
        monkeypatch.setenv(key, value)
    broker = SimpleNamespace(
        reconcile=lambda tenant: {"status": "matched"},
        protection_coverage=lambda tenant: {"status": "complete"},
        get_starting_capital=lambda tenant: D(100000),
    )
    briefs = tuple(
        FounderExecutionBrief(
            datetime(2026, 9, 21, 4, 0, tzinfo=timezone.utc),
            MarketState.REGULAR_HOURS, symbol, "PRESERVE_CAPITAL", (), "test", (),
            ("price=FRESH",),
        )
        for _, symbol in module.load_directives()[1]
    )
    traces = tuple(
        SimpleNamespace(
            subject=symbol,
            generated_at=datetime(2026, 9, 21, 4, 0, tzinfo=timezone.utc),
            provenance={"mode": "llm" if symbol != "SILVERBEES" else "llm_unavailable"},
        )
        for _, symbol in module.load_directives()[1]
    )
    logger = SimpleNamespace(traces=lambda: traces)
    runner = SimpleNamespace(
        daemon=SimpleNamespace(
            tracker=SimpleNamespace(broker=broker),
            briefs=briefs,
            scheduler=SimpleNamespace(
                pipeline=SimpleNamespace(runtime=SimpleNamespace(xai_logger=logger))
            ),
        ),
        intraday_warmup_status={symbol: {"status": "ready", "bars": 60, "updatedAt": "x"}
                                for _, symbol in module.load_directives()[1]},
    )

    report = module.runtime_certification(runner)

    assert report["ready"] is False
    assert report["reasons"] == ["five_symbol_llm_inference_not_observed"]
    assert report["inferenceModeBySymbol"]["SILVERBEES"] == "llm_unavailable"


def test_runtime_certification_reads_actual_starting_capital(monkeypatch):
    import importlib.util
    from pathlib import Path

    script = Path(__file__).parents[1] / "scripts/india_paper_runtime.py"
    spec = importlib.util.spec_from_file_location("india_runtime_capital_certification", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for key, value in {
        "TRADING_LIVE_MONEY_ACTIVE": "false",
        "PRAMANA_RELEASE_REVISION": "abc123",
        "PRAMANA_ZERODHA_SYMBOLS_JSON": '{"1":"INFY","2":"TCS","3":"RELIANCE","4":"GOLDBEES","5":"SILVERBEES"}',
        "PRAMANA_SESSION_FLATTEN_MINUTES": "15",
        "PRAMANA_DAILY_HISTORY_PROVIDER": "kite",
        "PRAMANA_REQUIRE_BOOK_RISK_GATES": "true",
        "PRAMANA_OVERNIGHT_GROSS_CAP": "0.25",
        "PRAMANA_OVERNIGHT_CLOSING_WINDOW_MINUTES": "15",
    }.items():
        monkeypatch.setenv(key, value)
    broker = SimpleNamespace(
        reconcile=lambda tenant: {"status": "matched"},
        protection_coverage=lambda tenant: {"status": "complete"},
        get_starting_capital=lambda tenant: D(99999),
    )
    at = datetime(2026, 9, 21, 4, 0, tzinfo=timezone.utc)
    briefs = tuple(
        FounderExecutionBrief(
            at, MarketState.REGULAR_HOURS, symbol, "PRESERVE_CAPITAL", (), "test", (),
            ("price=FRESH",),
        )
        for _, symbol in module.load_directives()[1]
    )
    traces = tuple(
        SimpleNamespace(subject=symbol, generated_at=at, provenance={"mode": "llm"})
        for _, symbol in module.load_directives()[1]
    )
    logger = SimpleNamespace(traces=lambda: traces)
    runner = SimpleNamespace(
        daemon=SimpleNamespace(
            tracker=SimpleNamespace(broker=broker),
            briefs=briefs,
            scheduler=SimpleNamespace(
                pipeline=SimpleNamespace(runtime=SimpleNamespace(xai_logger=logger))
            ),
        ),
        intraday_warmup_status={symbol: {"status": "ready", "bars": 60, "updatedAt": "x"}
                                for _, symbol in module.load_directives()[1]},
    )

    report = module.runtime_certification(runner)

    assert report["ready"] is False
    assert report["startingCapital"] == "99999"
    assert report["reasons"] == ["starting_capital_not_100000"]


def test_runtime_certification_rejects_old_llm_trace(monkeypatch):
    import importlib.util
    from pathlib import Path

    script = Path(__file__).parents[1] / "scripts/india_paper_runtime.py"
    spec = importlib.util.spec_from_file_location("india_runtime_trace_age_certification", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for key, value in {
        "TRADING_LIVE_MONEY_ACTIVE": "false",
        "PRAMANA_RELEASE_REVISION": "abc123",
        "PRAMANA_ZERODHA_SYMBOLS_JSON": '{"1":"INFY","2":"TCS","3":"RELIANCE","4":"GOLDBEES","5":"SILVERBEES"}',
        "PRAMANA_SESSION_FLATTEN_MINUTES": "15",
        "PRAMANA_DAILY_HISTORY_PROVIDER": "kite",
        "PRAMANA_REQUIRE_BOOK_RISK_GATES": "true",
        "PRAMANA_OVERNIGHT_GROSS_CAP": "0.25",
        "PRAMANA_OVERNIGHT_CLOSING_WINDOW_MINUTES": "15",
    }.items():
        monkeypatch.setenv(key, value)
    broker = SimpleNamespace(
        reconcile=lambda tenant: {"status": "matched"},
        protection_coverage=lambda tenant: {"status": "complete"},
        get_starting_capital=lambda tenant: D(100000),
    )
    at = datetime(2026, 9, 21, 4, 0, tzinfo=timezone.utc)
    briefs = tuple(
        FounderExecutionBrief(
            at, MarketState.REGULAR_HOURS, symbol, "PRESERVE_CAPITAL", (), "test", (),
            ("price=FRESH",),
        )
        for _, symbol in module.load_directives()[1]
    )
    traces = tuple(
        SimpleNamespace(
            subject=symbol,
            generated_at=at - timedelta(minutes=10),
            provenance={"mode": "llm"},
        )
        for _, symbol in module.load_directives()[1]
    )
    logger = SimpleNamespace(traces=lambda: traces)
    runner = SimpleNamespace(
        daemon=SimpleNamespace(
            tracker=SimpleNamespace(broker=broker),
            briefs=briefs,
            scheduler=SimpleNamespace(
                pipeline=SimpleNamespace(runtime=SimpleNamespace(xai_logger=logger))
            ),
        ),
        intraday_warmup_status={symbol: {"status": "ready", "bars": 60, "updatedAt": "x"}
                                for _, symbol in module.load_directives()[1]},
    )

    report = module.runtime_certification(runner)

    assert report["ready"] is False
    assert report["inferenceModeBySymbol"] == {}
    assert report["reasons"] == ["five_symbol_llm_inference_not_observed"]
