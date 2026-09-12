from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from quant_ai.agents.swarm_runtime import SwarmPaperTradingService
from quant_ai.domain.models import AssetClass, Instrument, Market, PortfolioSnapshot, RiskMode
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.execution.scheduler import AutonomousCadenceScheduler
from quant_ai.execution.session import MarketCalendar, MarketState
from quant_ai.intelligence.failover import (
    FailoverMarketDataFeed,
    FailoverNewsProvider,
    ProviderCategory,
    ProviderFailoverRegistry,
)
from quant_ai.intelligence.pipeline import SwarmMarketAnalysisPipeline
from quant_ai.intelligence.sandbox import (
    SandboxFundamentalDataProvider,
    SandboxMacroIndicatorProvider,
    SandboxNewsSentimentProvider,
)
from quant_ai.marketdata.feed import UsaSandboxMarketDataFeed
from quant_ai.planning.capital import CapitalGoalEngine, CapitalPlanRequest


class BrokenProvider:
    provider_id = "broken-primary"

    def fetch(self, *args, **kwargs):
        raise TimeoutError("primary_timeout")

    def fetch_ohlcv(self, *args, **kwargs):
        raise TimeoutError("primary_timeout")

    def latest_tick(self, *args, **kwargs):
        raise TimeoutError("primary_timeout")


def instrument() -> Instrument:
    return Instrument("AAPL", Market.USA, AssetClass.EQUITY, "USD", "NASDAQ")


def plan():
    return CapitalGoalEngine().recommend(CapitalPlanRequest(
        Decimal(100000), Decimal("0.82"), Decimal("0.20"),
        expected_edge=Decimal("0.02"), requested_mode=RiskMode.BALANCED,
    ))


def portfolio() -> PortfolioSnapshot:
    return PortfolioSnapshot(
        Decimal(100000), Decimal(0), Decimal(0), peak_equity=Decimal(100000)
    )


def test_failover_registry_uses_secondary_without_crashing() -> None:
    registry = ProviderFailoverRegistry()
    registry.register(
        ProviderCategory.NEWS,
        BrokenProvider(),
        SandboxNewsSentimentProvider(),
    )
    provider = FailoverNewsProvider(registry)
    items = provider.fetch("AAPL", datetime(2026, 9, 14, 14, tzinfo=timezone.utc))
    assert items
    assert registry.last_attempts[0].provider_id == "broken-primary"


def test_failover_market_feed_falls_back_to_sandbox() -> None:
    registry = ProviderFailoverRegistry()
    registry.register(
        ProviderCategory.PRICE,
        BrokenProvider(),
        UsaSandboxMarketDataFeed(),
    )
    feed = FailoverMarketDataFeed(registry)
    candles = feed.fetch_ohlcv(
        instrument(),
        datetime(2026, 9, 14, 13, tzinfo=timezone.utc),
        datetime(2026, 9, 14, 14, tzinfo=timezone.utc),
    )
    assert len(candles) == 60


def test_market_calendar_sessions_weekends_and_holidays() -> None:
    calendar = MarketCalendar({Market.USA: frozenset({date(2026, 9, 15)})})
    regular = datetime(2026, 9, 14, 14, tzinfo=timezone.utc)
    assert calendar.state(Market.USA, regular) == MarketState.REGULAR_HOURS
    pre = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
    assert calendar.state(Market.USA, pre) == MarketState.PRE_MARKET
    saturday = datetime(2026, 9, 12, 14, tzinfo=timezone.utc)
    assert calendar.state(Market.USA, saturday) == MarketState.CLOSED
    holiday = datetime(2026, 9, 15, 14, tzinfo=timezone.utc)
    assert calendar.state(Market.USA, holiday) == MarketState.CLOSED


def test_closed_market_runs_macro_geo_only_and_never_trades(tmp_path) -> None:
    broker = PaperBrokerService(
        tmp_path / "closed.db", starting_capital=Decimal(100000), slippage_bps=Decimal(0)
    )
    pipeline = SwarmMarketAnalysisPipeline(
        UsaSandboxMarketDataFeed(),
        SandboxNewsSentimentProvider(),
        SandboxFundamentalDataProvider(),
        SandboxMacroIndicatorProvider(),
        runtime=SwarmPaperTradingService(broker=broker),
    )
    scheduler = AutonomousCadenceScheduler(pipeline)
    brief = scheduler.run_tick(
        instrument(),
        datetime(2026, 9, 12, 14, tzinfo=timezone.utc),
        plan(),
        portfolio(),
        quantity=10,
        country="USA",
        tenant_id="closed",
    )
    assert brief.market_state == MarketState.CLOSED
    assert brief.mode == "OFF_HOURS_MACRO_GEO_SWEEP"
    assert not brief.paper_order_ids
    assert not broker.ledger_entries("closed")


def test_open_market_cadence_creates_founder_brief_and_paper_fill(tmp_path) -> None:
    broker = PaperBrokerService(
        tmp_path / "open.db", starting_capital=Decimal(100000), slippage_bps=Decimal(0)
    )
    pipeline = SwarmMarketAnalysisPipeline(
        UsaSandboxMarketDataFeed(),
        SandboxNewsSentimentProvider(),
        SandboxFundamentalDataProvider(),
        SandboxMacroIndicatorProvider(),
        runtime=SwarmPaperTradingService(broker=broker),
    )
    scheduler = AutonomousCadenceScheduler(pipeline, cadence=timedelta(minutes=10))
    now = datetime(2026, 9, 14, 14, tzinfo=timezone.utc)
    assert scheduler.is_due(now)
    brief = scheduler.run_tick(
        instrument(), now, plan(), portfolio(), quantity=10, country="USA", tenant_id="open"
    )
    assert brief.market_state == MarketState.REGULAR_HOURS
    assert brief.paper_order_ids
    assert brief.paper_order_ids[0].startswith("PAPER-")
    assert len(broker.ledger_entries("open")) == 1
    assert not scheduler.is_due(now + timedelta(minutes=5))
    assert scheduler.is_due(now + timedelta(minutes=10))
    assert "PAPER-" in brief.to_json()


def test_full_pipeline_survives_primary_provider_failures(tmp_path) -> None:
    from quant_ai.intelligence.failover import (
        FailoverFundamentalProvider,
        FailoverMacroProvider,
    )

    registry = ProviderFailoverRegistry()
    registry.register(ProviderCategory.PRICE, BrokenProvider(), UsaSandboxMarketDataFeed())
    registry.register(ProviderCategory.NEWS, BrokenProvider(), SandboxNewsSentimentProvider())
    registry.register(
        ProviderCategory.FUNDAMENTALS,
        BrokenProvider(),
        SandboxFundamentalDataProvider(),
    )
    registry.register(ProviderCategory.MACRO, BrokenProvider(), SandboxMacroIndicatorProvider())
    broker = PaperBrokerService(
        tmp_path / "failover.db", starting_capital=Decimal(100000), slippage_bps=Decimal(0)
    )
    pipeline = SwarmMarketAnalysisPipeline(
        FailoverMarketDataFeed(registry),
        FailoverNewsProvider(registry),
        FailoverFundamentalProvider(registry),
        FailoverMacroProvider(registry),
        runtime=SwarmPaperTradingService(broker=broker),
    )
    result = pipeline.run(
        instrument(),
        datetime(2026, 9, 14, 14, tzinfo=timezone.utc),
        plan(),
        portfolio(),
        quantity=10,
        country="USA",
        tenant_id="failover",
    )
    assert result.execution.fill is not None
    assert result.execution.fill.order_id.startswith("PAPER-")
    assert len(broker.ledger_entries("failover")) == 1


def test_all_provider_failure_fails_closed_neutral_without_crash(tmp_path) -> None:
    from quant_ai.agents.contracts import Stance
    from quant_ai.intelligence.failover import (
        FailoverFundamentalProvider,
        FailoverMacroProvider,
    )

    registry = ProviderFailoverRegistry()
    for category in ProviderCategory:
        registry.register(category, BrokenProvider())
    broker = PaperBrokerService(
        tmp_path / "neutral.db", starting_capital=Decimal(100000), slippage_bps=Decimal(0)
    )
    pipeline = SwarmMarketAnalysisPipeline(
        FailoverMarketDataFeed(registry),
        FailoverNewsProvider(registry),
        FailoverFundamentalProvider(registry),
        FailoverMacroProvider(registry),
        runtime=SwarmPaperTradingService(broker=broker),
    )
    result = pipeline.run(
        instrument(),
        datetime(2026, 9, 14, 14, tzinfo=timezone.utc),
        plan(),
        portfolio(),
        quantity=10,
        country="USA",
        tenant_id="neutral",
    )
    assert all(item.stance == Stance.NEUTRAL for item in result.evidence)
    assert result.execution.fill is None
    assert not broker.ledger_entries("neutral")
