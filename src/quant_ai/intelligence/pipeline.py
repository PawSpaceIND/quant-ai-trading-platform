from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from quant_ai.agents.contracts import AgentEvidence, Stance
from quant_ai.agents.swarm import (
    AgentAnalysisRequest,
    CommodityYieldAgent,
    GeopoliticalAnalystAgent,
    IndianEquitiesAgent,
    TechnicalQuantAgent,
    USEquitiesAgent,
)
from quant_ai.agents.swarm_runtime import SwarmExecutionResult, SwarmPaperTradingService
from quant_ai.analytics.metrics import PerformanceMetrics, summarize_performance
from quant_ai.domain.models import Instrument, PortfolioSnapshot
from quant_ai.intelligence.freshness import (
    DataCategory,
    FreshnessResult,
    FreshnessValidator,
    IntelligenceDataCache,
)
from quant_ai.intelligence.providers import (
    FundamentalDataProvider,
    MacroIndicatorProvider,
    NewsSentimentProvider,
)
from quant_ai.intelligence.regime import MarketRegimeDetector, RegimeAssessment
from quant_ai.marketdata.feed import MarketDataFeed
from quant_ai.marketdata.ticker_stream import LiveTick
from quant_ai.orchestration.cadence import CadenceMarketReader
from quant_ai.planning.capital import CapitalPlan


@dataclass(frozen=True)
class PipelineFreshness:
    price: FreshnessResult
    news: FreshnessResult
    macro: FreshnessResult
    fundamentals: FreshnessResult


@dataclass(frozen=True)
class MarketAnalysisResult:
    evidence: tuple[AgentEvidence, ...]
    freshness: PipelineFreshness
    requested_quantity: int
    effective_quantity: int
    conflict_ratio: Decimal
    execution: SwarmExecutionResult
    regime: RegimeAssessment
    analytics: PerformanceMetrics


class SwarmMarketAnalysisPipeline:
    def __init__(
        self,
        market_feed: MarketDataFeed,
        news: NewsSentimentProvider,
        fundamentals: FundamentalDataProvider,
        macro: MacroIndicatorProvider,
        *,
        runtime: SwarmPaperTradingService | None = None,
        freshness: FreshnessValidator | None = None,
        regime_detector: MarketRegimeDetector | None = None,
        tick_reader: CadenceMarketReader | None = None,
    ) -> None:
        self.market_feed = market_feed
        self.news = news
        self.fundamentals = fundamentals
        self.macro = macro
        self.runtime = runtime or SwarmPaperTradingService()
        self.freshness = freshness or FreshnessValidator()
        self.regime_detector = regime_detector or MarketRegimeDetector()
        self.tick_reader = tick_reader
        self.cache = IntelligenceDataCache()
        self.agents = (
            GeopoliticalAnalystAgent(), CommodityYieldAgent(),
            IndianEquitiesAgent(), USEquitiesAgent(), TechnicalQuantAgent(),
        )

    def run(
        self,
        instrument: Instrument,
        now: datetime,
        plan: CapitalPlan,
        portfolio: PortfolioSnapshot,
        *,
        quantity: int,
        country: str,
        tenant_id: str = "default",
        country_exposure: dict[str, Decimal] | None = None,
    ) -> MarketAnalysisResult:
        candles = self.market_feed.fetch_ohlcv(
            instrument, now - timedelta(minutes=60), now, "1m"
        )
        news = self.news.fetch(instrument.symbol, now)
        geopolitical = self.news.fetch("GEOPOLITICAL", now)
        fundamentals = self.fundamentals.fetch(instrument.symbol, now)
        macro = self.macro.fetch(("US10Y", "INDIA10Y", "BRENT", "GOLD", "DXY"), now)

        last_price_at = candles[-1].timestamp if candles else None
        latest_news_at = max((item.published_at for item in news + geopolitical), default=None)
        required_macro = {"US10Y", "INDIA10Y", "BRENT", "GOLD", "DXY"}
        macro_at = macro.observed_at if required_macro <= macro.indicators.keys() else None
        fundamentals_at = fundamentals.observed_at if fundamentals.metrics else None
        states = PipelineFreshness(
            self.freshness.validate(DataCategory.PRICE, last_price_at, now),
            self.freshness.validate(DataCategory.NEWS, latest_news_at, now),
            self.freshness.validate(DataCategory.MACRO, macro_at, now),
            self.freshness.validate(DataCategory.FUNDAMENTAL, fundamentals_at, now),
        )
        if candles:
            self.cache.put(f"price:{instrument.symbol}", candles, last_price_at)
        if news or geopolitical:
            self.cache.put(f"news:{instrument.symbol}", news + geopolitical, latest_news_at)
        if macro_at is not None:
            self.cache.put("macro:core", macro, macro_at)
        if fundamentals_at is not None:
            self.cache.put(f"fundamentals:{instrument.symbol}", fundamentals, fundamentals_at)
        closes = tuple(c.close for c in candles)
        regime = self.regime_detector.detect(candles)
        effective_plan = self.regime_detector.apply_to_plan(plan, regime)
        returns = tuple(
            (after - before) / before
            for before, after in zip(closes, closes[1:])
            if before > 0
        )
        curve = [Decimal(100)]
        for item in returns:
            curve.append(curve[-1] * (Decimal(1) + item))
        analytics = summarize_performance(returns, tuple(curve), returns)
        technical = self._technical_metrics(closes)
        equity_news = self._mean(tuple(item.sentiment for item in news))
        geopolitical_sentiment = self._mean(tuple(item.sentiment for item in geopolitical))
        macro_metrics = self._macro_metrics(macro.indicators)

        common = dict(fundamentals.metrics)
        common.update(technical)
        common.update(macro_metrics)
        common["equity_news_sentiment"] = equity_news
        common["news_sentiment"] = geopolitical_sentiment
        common["conflict_risk"] = max(Decimal(0), -geopolitical_sentiment)
        common["sanctions_risk"] = max(Decimal(0), -geopolitical_sentiment / Decimal(2))

        max_age = max(item.age_seconds or 0 for item in (states.price, states.news, states.macro, states.fundamentals))
        requests = []
        for agent in self.agents:
            required = self._required_freshness(agent.agent_id, states)
            metrics = dict(common)
            metrics["freshness_multiplier"] = required
            requests.append(AgentAnalysisRequest(
                instrument.symbol, instrument.market, instrument.asset_class, now, metrics, max_age
            ))
        evidence = tuple(agent.analyze(request) for agent, request in zip(self.agents, requests))
        conflict = self._conflict_ratio(evidence)
        effective_quantity = max(1, quantity // 2) if conflict >= Decimal("0.40") else quantity
        root_request = requests[-1]
        reference_price = closes[-1] if closes else Decimal(0)
        stop = reference_price * (Decimal(1) - plan.stop_loss_fraction) if reference_price > 0 else None
        take_profit = reference_price * (Decimal(1) + plan.take_profit_fraction) if reference_price > 0 else None
        execution = self.runtime.execute(
            root_request, evidence, effective_plan, portfolio,
            quantity=effective_quantity, reference_price=reference_price,
            stop_price=stop, take_profit_price=take_profit, country=country,
            country_exposure=country_exposure, tenant_id=tenant_id,
        )
        return MarketAnalysisResult(
            evidence, states, quantity, effective_quantity, conflict, execution, regime, analytics
        )

    async def run_async(
        self,
        instrument: Instrument,
        now: datetime,
        plan: CapitalPlan,
        portfolio: PortfolioSnapshot,
        *,
        quantity: int,
        country: str,
        tenant_id: str = "default",
        country_exposure: dict[str, Decimal] | None = None,
    ) -> MarketAnalysisResult:
        candles = self.market_feed.fetch_ohlcv(
            instrument, now - timedelta(minutes=60), now, "1m"
        )
        news = self.news.fetch(instrument.symbol, now)
        geopolitical = self.news.fetch("GEOPOLITICAL", now)
        fundamentals = self.fundamentals.fetch(instrument.symbol, now)
        macro = self.macro.fetch(("US10Y", "INDIA10Y", "BRENT", "GOLD", "DXY"), now)

        last_price_at = candles[-1].timestamp if candles else None
        latest_news_at = max((item.published_at for item in news + geopolitical), default=None)
        required_macro = {"US10Y", "INDIA10Y", "BRENT", "GOLD", "DXY"}
        macro_at = macro.observed_at if required_macro <= macro.indicators.keys() else None
        fundamentals_at = fundamentals.observed_at if fundamentals.metrics else None
        states = PipelineFreshness(
            self.freshness.validate(DataCategory.PRICE, last_price_at, now),
            self.freshness.validate(DataCategory.NEWS, latest_news_at, now),
            self.freshness.validate(DataCategory.MACRO, macro_at, now),
            self.freshness.validate(DataCategory.FUNDAMENTAL, fundamentals_at, now),
        )
        closes = tuple(c.close for c in candles)
        regime = self.regime_detector.detect(candles)
        effective_plan = self.regime_detector.apply_to_plan(plan, regime)
        returns = tuple(
            (after - before) / before
            for before, after in zip(closes, closes[1:])
            if before > 0
        )
        curve = [Decimal(100)]
        for item in returns:
            curve.append(curve[-1] * (Decimal(1) + item))
        analytics = summarize_performance(returns, tuple(curve), returns)
        technical = self._technical_metrics(closes)
        common = dict(fundamentals.metrics)
        common.update(technical)
        common.update(self._macro_metrics(macro.indicators))
        common["equity_news_sentiment"] = self._mean(tuple(item.sentiment for item in news))
        geo_sentiment = self._mean(tuple(item.sentiment for item in geopolitical))
        common["news_sentiment"] = geo_sentiment
        common["conflict_risk"] = max(Decimal(0), -geo_sentiment)
        common["sanctions_risk"] = max(Decimal(0), -geo_sentiment / Decimal(2))

        market_tick, market_data_veto = self._market_tick_status(instrument.symbol, now)
        if market_tick is not None:
            common["live_ltp"] = market_tick.ltp
            common["live_volume"] = market_tick.volume
            if market_tick.spread is not None:
                common["live_bid_ask_spread"] = market_tick.spread

        max_age = max(
            item.age_seconds or 0
            for item in (states.price, states.news, states.macro, states.fundamentals)
        )
        requests = []
        for agent in self.agents:
            required = self._required_freshness(agent.agent_id, states)
            metrics = dict(common)
            metrics["freshness_multiplier"] = required
            requests.append(
                AgentAnalysisRequest(
                    instrument.symbol, instrument.market, instrument.asset_class, now, metrics, max_age
                )
            )
        evidence = tuple(agent.analyze(request) for agent, request in zip(self.agents, requests))
        conflict = self._conflict_ratio(evidence)
        effective_quantity = max(1, quantity // 2) if conflict >= Decimal("0.40") else quantity
        root_request = requests[-1]
        reference_price = (
            market_tick.ltp if market_tick is not None and market_tick.ltp > 0
            else (closes[-1] if closes else Decimal(0))
        )
        stop = (
            reference_price * (Decimal(1) - plan.stop_loss_fraction)
            if reference_price > 0 else None
        )
        take_profit = (
            reference_price * (Decimal(1) + plan.take_profit_fraction)
            if reference_price > 0 else None
        )
        execution = await self.runtime.execute_async(
            root_request, evidence, effective_plan, portfolio,
            quantity=effective_quantity, reference_price=reference_price,
            stop_price=stop, take_profit_price=take_profit, country=country,
            market_tick=market_tick, preflight_veto_reason=market_data_veto,
            country_exposure=country_exposure, tenant_id=tenant_id,
        )
        return MarketAnalysisResult(
            evidence, states, quantity, effective_quantity, conflict, execution, regime, analytics
        )

    def _market_tick_status(
        self, symbol: str, now: datetime
    ) -> tuple[LiveTick | None, str | None]:
        if self.tick_reader is None:
            return None, None
        return self.tick_reader.market_data_status(symbol, now)

    def _latest_tick(self, symbol: str, now: datetime) -> LiveTick | None:
        tick, _ = self._market_tick_status(symbol, now)
        return tick

    @staticmethod
    def _required_freshness(agent_id: str, states: PipelineFreshness) -> Decimal:
        if agent_id == "geopolitical-analyst":
            return states.news.confidence_multiplier
        if agent_id == "commodity-yield":
            return states.macro.confidence_multiplier
        if agent_id in {"indian-equities", "us-equities"}:
            return min(states.news.confidence_multiplier, states.macro.confidence_multiplier, states.fundamentals.confidence_multiplier)
        return states.price.confidence_multiplier

    @staticmethod
    def _mean(values: tuple[Decimal, ...]) -> Decimal:
        return sum(values, Decimal(0)) / Decimal(len(values)) if values else Decimal(0)

    @staticmethod
    def _macro_metrics(values: dict[str, Decimal]) -> dict[str, Decimal]:
        us10y = values.get("US10Y", Decimal(0))
        brent = values.get("BRENT", Decimal(0))
        gold = values.get("GOLD", Decimal(0))
        dxy = values.get("DXY", Decimal(0))
        return {
            "us10y": us10y,
            "yield_change": (us10y - Decimal("4.0")) / Decimal(4),
            "brent_change": (brent - Decimal(75)) / Decimal(75),
            "gold_change": (gold - Decimal(2400)) / Decimal(2400),
            "dxy_change": (dxy - Decimal(102)) / Decimal(102),
        }

    @staticmethod
    def _technical_metrics(closes: tuple[Decimal, ...]) -> dict[str, Decimal]:
        if len(closes) < 50:
            return {"sma_spread": Decimal(0), "rsi": Decimal(50), "momentum": Decimal(0)}
        sma20 = sum(closes[-20:], Decimal(0)) / Decimal(20)
        sma50 = sum(closes[-50:], Decimal(0)) / Decimal(50)
        spread = (sma20 - sma50) / sma50 if sma50 else Decimal(0)
        momentum = (closes[-1] - closes[-10]) / closes[-10] if closes[-10] else Decimal(0)
        gains = Decimal(0)
        losses = Decimal(0)
        for before, after in zip(closes[-15:-1], closes[-14:]):
            delta = after - before
            if delta > 0:
                gains += delta
            else:
                losses += -delta
        if losses == 0:
            rsi = Decimal(100) if gains > 0 else Decimal(50)
        else:
            rs = gains / losses
            rsi = Decimal(100) - Decimal(100) / (Decimal(1) + rs)
        return {"sma_spread": spread, "rsi": rsi, "momentum": momentum}

    @staticmethod
    def _conflict_ratio(evidence: tuple[AgentEvidence, ...]) -> Decimal:
        positive = sum(item.confidence for item in evidence if item.stance in {Stance.BUY, Stance.STRONG_BUY})
        negative = sum(item.confidence for item in evidence if item.stance in {Stance.SELL, Stance.STRONG_SELL, Stance.AVOID})
        directional = positive + negative
        if directional <= 0:
            return Decimal(0)
        return min(positive, negative) / directional
