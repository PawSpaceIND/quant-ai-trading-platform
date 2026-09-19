from datetime import datetime, timedelta, timezone
from decimal import Decimal

from quant_ai.agents.contracts import Stance
from quant_ai.agents.swarm_runtime import SwarmPaperTradingService
from quant_ai.domain.models import AssetClass, Instrument, Market, PortfolioSnapshot, RiskMode
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.intelligence.freshness import (
    DataCategory,
    FreshnessResult,
    FreshnessState,
    FreshnessValidator,
)
from quant_ai.intelligence.pipeline import PipelineFreshness, SwarmMarketAnalysisPipeline
from quant_ai.intelligence.sandbox import (
    SandboxFundamentalDataProvider,
    SandboxMacroIndicatorProvider,
    SandboxNewsSentimentProvider,
)
from quant_ai.marketdata.feed import UsaSandboxMarketDataFeed
from quant_ai.planning.capital import CapitalGoalEngine, CapitalPlanRequest


def plan():
    return CapitalGoalEngine().recommend(CapitalPlanRequest(
        Decimal(100000), Decimal("0.82"), Decimal("0.20"),
        expected_edge=Decimal("0.02"), requested_mode=RiskMode.BALANCED,
    ))


def portfolio():
    return PortfolioSnapshot(Decimal(100000), Decimal(0), Decimal(0), peak_equity=Decimal(100000))


def instrument():
    return Instrument("AAPL", Market.USA, AssetClass.EQUITY, "USD", "NASDAQ")


def test_freshness_validator_ttls_and_penalties() -> None:
    validator = FreshnessValidator()
    now = datetime.now(timezone.utc)
    fresh = validator.validate(DataCategory.NEWS, now, now)
    stale = validator.validate(DataCategory.NEWS, now - timedelta(hours=1), now)
    missing = validator.validate(DataCategory.MACRO, None, now)
    assert fresh.state == FreshnessState.FRESH
    assert fresh.confidence_multiplier == Decimal(1)
    assert stale.state == FreshnessState.STALE
    assert stale.confidence_multiplier < Decimal(1)
    assert missing.state == FreshnessState.MISSING
    assert missing.confidence_multiplier == 0



def _freshness_result(state: FreshnessState, multiplier: str) -> FreshnessResult:
    age = None if state is FreshnessState.MISSING else 10
    return FreshnessResult(state, age, 3600, Decimal(multiplier))


def test_specialist_freshness_dependencies_match_inputs() -> None:
    states = PipelineFreshness(
        _freshness_result(FreshnessState.FRESH, "1"),
        _freshness_result(FreshnessState.FRESH, "1"),
        _freshness_result(FreshnessState.MISSING, "0"),
        _freshness_result(FreshnessState.FRESH, "1"),
    )
    # India valuation does not consume macro metrics and must not be muted by a macro outage.
    assert SwarmMarketAnalysisPipeline._required_freshness("indian-equities", states) == Decimal(1)
    # Commodity and US equity agents do consume macro evidence and remain capital-preserving.
    assert SwarmMarketAnalysisPipeline._required_freshness("commodity-yield", states) == Decimal(0)
    assert SwarmMarketAnalysisPipeline._required_freshness("us-equities", states) == Decimal(0)
    assert "macro=MISSING" in SwarmMarketAnalysisPipeline._freshness_diagnostic("commodity-yield", states)


def test_indian_equities_is_silenced_when_its_actual_fundamentals_are_missing() -> None:
    states = PipelineFreshness(
        _freshness_result(FreshnessState.FRESH, "1"),
        _freshness_result(FreshnessState.FRESH, "1"),
        _freshness_result(FreshnessState.FRESH, "1"),
        _freshness_result(FreshnessState.MISSING, "0"),
    )
    assert SwarmMarketAnalysisPipeline._required_freshness("indian-equities", states) == Decimal(0)
    assert "fundamentals=MISSING" in SwarmMarketAnalysisPipeline._freshness_diagnostic("indian-equities", states)

def test_stale_news_penalizes_confidence_and_blocks_execution(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    broker = PaperBrokerService(tmp_path / "stale.db", starting_capital=Decimal(100000), slippage_bps=Decimal(0))
    pipeline = SwarmMarketAnalysisPipeline(
        UsaSandboxMarketDataFeed(),
        SandboxNewsSentimentProvider(age_seconds=7200),
        SandboxFundamentalDataProvider(),
        SandboxMacroIndicatorProvider(),
        runtime=SwarmPaperTradingService(broker=broker),
    )
    result = pipeline.run(instrument(), now, plan(), portfolio(), quantity=10, country="USA", tenant_id="stale")
    assert result.freshness.news.state == FreshnessState.STALE
    geo = next(item for item in result.evidence if item.agent_id == "geopolitical-analyst")
    assert geo.confidence < Decimal("0.40")
    assert geo.stance == Stance.NEUTRAL
    assert result.execution.fill is None
    assert not broker.ledger_entries("stale")


class ConflictMacro(SandboxMacroIndicatorProvider):
    def fetch(self, indicators, now):
        snapshot = super().fetch(indicators, now)
        values = dict(snapshot.indicators)
        values.update({"US10Y": Decimal("6.5"), "BRENT": Decimal(140), "USD_BROAD": Decimal(120), "GOLD": Decimal(2300)})
        return type(snapshot)(values, snapshot.observed_at)


class ConflictNews(SandboxNewsSentimentProvider):
    def fetch(self, subject, now):
        base = super().fetch(subject, now)
        if subject.upper() == "GEOPOLITICAL":
            item = base[0]
            return (type(item)(item.subject, "conflict escalates", Decimal("-0.85"), item.source, item.published_at),)
        return base


def test_conflicting_macro_and_equity_evidence_preserves_or_reduces_capital(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    broker = PaperBrokerService(tmp_path / "conflict.db", starting_capital=Decimal(100000), slippage_bps=Decimal(0))
    pipeline = SwarmMarketAnalysisPipeline(
        UsaSandboxMarketDataFeed(), ConflictNews(), SandboxFundamentalDataProvider(), ConflictMacro(),
        runtime=SwarmPaperTradingService(broker=broker),
    )
    result = pipeline.run(instrument(), now, plan(), portfolio(), quantity=20, country="USA", tenant_id="conflict")
    assert result.conflict_ratio > 0 or result.execution.proposal.side is None
    assert result.effective_quantity <= 20
    if result.execution.proposal.side is None or not result.execution.risk_decision.approved:
        assert result.execution.fill is None
    else:
        assert result.effective_quantity < result.requested_quantity


def test_green_ingestion_to_paper_loop_creates_sqlite_fill(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    broker = PaperBrokerService(tmp_path / "green.db", starting_capital=Decimal(100000), slippage_bps=Decimal(0))
    pipeline = SwarmMarketAnalysisPipeline(
        UsaSandboxMarketDataFeed(), SandboxNewsSentimentProvider(),
        SandboxFundamentalDataProvider(), SandboxMacroIndicatorProvider(),
        runtime=SwarmPaperTradingService(broker=broker),
    )
    result = pipeline.run(instrument(), now, plan(), portfolio(), quantity=10, country="USA", tenant_id="green")
    assert all(item.state == FreshnessState.FRESH for item in (
        result.freshness.price, result.freshness.news, result.freshness.macro, result.freshness.fundamentals,
    ))
    assert result.execution.risk_decision.approved
    assert result.execution.fill is not None
    assert result.execution.fill.order_id.startswith("PAPER-")
    assert len(broker.ledger_entries("green")) == 1


def test_pipeline_populates_validated_data_cache(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    broker = PaperBrokerService(tmp_path / "cache.db", starting_capital=Decimal(100000), slippage_bps=Decimal(0))
    pipeline = SwarmMarketAnalysisPipeline(
        UsaSandboxMarketDataFeed(), SandboxNewsSentimentProvider(),
        SandboxFundamentalDataProvider(), SandboxMacroIndicatorProvider(),
        runtime=SwarmPaperTradingService(broker=broker),
    )
    pipeline.run(instrument(), now, plan(), portfolio(), quantity=5, country="USA", tenant_id="cache")
    assert pipeline.cache.get("price:AAPL") is not None
    assert pipeline.cache.get("news:AAPL") is not None
    assert pipeline.cache.get("macro:core") is not None
    assert pipeline.cache.get("fundamentals:AAPL") is not None
