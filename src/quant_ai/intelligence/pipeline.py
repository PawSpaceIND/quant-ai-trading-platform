from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal

from quant_ai.agents.contracts import (
    DEFAULT_MAX_TIMEFRAME_BARS,
    MAX_EVIDENCE_BARS,
    MAX_EVIDENCE_HEADLINES,
    MAX_LESSON_CHARS,
    MAX_LESSONS,
    MAX_TIMEFRAME_BARS,
    AgentEvidence,
    EvidenceBar,
    EvidenceContext,
    EvidenceHeadline,
    Stance,
)
from quant_ai.agents.swarm import (
    AgentAnalysisRequest,
    CommodityYieldAgent,
    GeopoliticalAnalystAgent,
    IndianEquitiesAgent,
    InstrumentBoundAnalysisRequest,
    LiquidityDeskAgent,
    RiskDeskAgent,
    TechnicalQuantAgent,
    USEquitiesAgent,
)
from quant_ai.agents.swarm_runtime import SwarmExecutionResult, SwarmPaperTradingService
from quant_ai.analytics.metrics import PerformanceMetrics, summarize_performance
from quant_ai.domain.models import Instrument, PortfolioSnapshot, Side
from quant_ai.execution.session import intraday_periods_per_year
from quant_ai.intelligence.freshness import (
    DataCategory,
    FreshnessResult,
    FreshnessValidator,
    IntelligenceDataCache,
)
from quant_ai.intelligence.headline_sentiment import (
    HeadlineScore,
    HeadlineSentimentScorer,
    keyword_score,
    normalize_headline,
)
from quant_ai.intelligence.providers import (
    MACRO_CORE_INDICATORS,
    MACRO_INDICATORS,
    FundamentalDataProvider,
    FundamentalSnapshot,
    MacroIndicatorProvider,
    MacroSnapshot,
    NewsSentimentProvider,
    NewsSignal,
)
from quant_ai.intelligence.regime import (
    MarketRegimeDetector,
    RegimeAssessment,
    RegimeSummary,
    classify,
    primary_regime,
)
from quant_ai.intelligence.regime_observation import RegimeObservationStore
from quant_ai.marketdata.feed import MarketDataFeed
from quant_ai.marketdata.models import Candle
from quant_ai.marketdata.ticker_stream import LiveTick
from quant_ai.marketdata.timeframes import DailyHistoryProvider, aggregate, venue_for
from quant_ai.orchestration.cadence import CadenceMarketReader
from quant_ai.planning.capital import CapitalPlan
from quant_ai.portfolio.sizing import PositionSizer

LOGGER = logging.getLogger("quant_ai.pipeline")

# Higher-timeframe context. The 15-minute bars come from a wider 1-minute window than the
# 60-minute one the specialists, the analytics and the plan score, so adding them moves no
# sizing input; 30 hours reaches the previous session on a weekday for both venues, and the
# live tick feed answers it from the bars it already holds in memory.
INTRADAY_TIMEFRAME = "15m"
INTRADAY_TIMEFRAME_MINUTES = 15
DAILY_TIMEFRAME = "1d"
INTRADAY_HISTORY_WINDOW = timedelta(hours=30)
# A provider or feed fault must never break the cadence: context is evidence, not a gate.
_CONTEXT_FAILURES = (
    TimeoutError, OSError, RuntimeError, ValueError, TypeError, LookupError, AttributeError,
    ArithmeticError,
)
# The analytics block scores one-minute instrument returns, so it must annualise with the
# number of one-minute bars in a trading year at that venue - not with trading days.
ONE_MINUTE = timedelta(minutes=1)


def _one_minute_periods(instrument: Instrument) -> int:
    return intraday_periods_per_year(instrument.market, ONE_MINUTE)


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
    # The deterministic regime label the decision was made in; see ``MarketContext``.
    regime_summary: RegimeSummary | None = None
    # The metric vector handed to every specialist on this tick. It is computed here and
    # was, until now, discarded once the agents had read it - which left the journal
    # holding conclusions with no record of what produced them. Carried so the decision
    # journal can store it; nothing in the decision path reads it back.
    features: Mapping[str, Decimal | str] = field(default_factory=dict)


@dataclass(frozen=True)
class MarketContext:
    """Closed higher-timeframe bars and regime summaries for one cadence tick.

    ``primary`` is the daily summary when the history provider returned enough closed
    sessions, else the 15-minute summary, else ``insufficient_history``; both summaries
    stay available as evidence.
    """

    intraday_bars: tuple[Candle, ...]
    daily_bars: tuple[Candle, ...]
    intraday: RegimeSummary
    daily: RegimeSummary
    primary: RegimeSummary

    def metrics(self) -> dict[str, Decimal | str]:
        """The regime facts the deterministic specialists receive with the other metrics."""
        return {
            "regime_label": self.primary.label,
            "regime_timeframe": self.primary.timeframe,
            "regime_daily_label": self.daily.label,
            "regime_daily_bars_used": Decimal(self.daily.bars_used),
            "regime_daily_bars_available": Decimal(len(self.daily_bars)),
            "regime_intraday_label": self.intraday.label,
            "regime_intraday_bars_used": Decimal(self.intraday.bars_used),
            "regime_intraday_bars_available": Decimal(len(self.intraday_bars)),
            "regime_trend_strength": self.primary.trend_strength,
            "regime_volatility_ratio": self.primary.volatility_ratio,
        }

    def regime_evidence(self) -> tuple[tuple[str, Decimal | str], ...]:
        """``label`` and ``timeframe`` first (the provenance reads them), then the metrics."""
        pairs: list[tuple[str, Decimal | str]] = [
            ("label", self.primary.label),
            ("timeframe", self.primary.timeframe),
            *self.primary.as_evidence(),
        ]
        for summary in (self.daily, self.intraday):
            pairs.append((f"{summary.timeframe}_label", summary.label))
            pairs.append((f"{summary.timeframe}_bars", Decimal(summary.bars_used)))
        return tuple(pairs)

    def timeframe_evidence(self) -> tuple[tuple[str, tuple[EvidenceBar, ...]], ...]:
        """The newest closed bars per timeframe, bounded by the contract, oldest first."""
        series = ((INTRADAY_TIMEFRAME, self.intraday_bars), (DAILY_TIMEFRAME, self.daily_bars))
        return tuple(
            (
                name,
                tuple(
                    _evidence_bar(candle)
                    for candle in bars[-MAX_TIMEFRAME_BARS.get(name, DEFAULT_MAX_TIMEFRAME_BARS):]
                ),
            )
            for name, bars in series
        )


def _evidence_bar(candle: Candle) -> EvidenceBar:
    return EvidenceBar(
        candle.timestamp.isoformat(),
        candle.open, candle.high, candle.low, candle.close, candle.volume,
    )


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
        sizer: PositionSizer | None = None,
        news_window: timedelta = timedelta(hours=6),
        history: DailyHistoryProvider | None = None,
        lessons_provider: Callable[[], Sequence[str]] | None = None,
        intraday_window: timedelta = INTRADAY_HISTORY_WINDOW,
        headline_scorer: HeadlineSentimentScorer | None = None,
        bind_order_instruments: bool = False,
    ) -> None:
        if type(bind_order_instruments) is not bool:
            raise TypeError("bind_order_instruments_must_be_boolean")
        self.bind_order_instruments = bind_order_instruments
        if news_window <= timedelta(0):
            raise ValueError("news_window must be positive")
        if intraday_window <= timedelta(0):
            raise ValueError("intraday_window must be positive")
        self.news_window = news_window
        # Read-only closed daily bars for regime context. None means no daily context and
        # no I/O; the regime then comes from the 15-minute bars, or abstains.
        self.history = history
        # Operator-approved post-mortem lessons. They are prior-session notes a human
        # signed off, never model self-talk, and they reach the prompt inside the block
        # that is labelled as data rather than instructions.
        self.lessons_provider = lessons_provider
        # Headline sentiment for the async cadence. The default keyword-only scorer performs
        # no I/O, so the pipeline behaves exactly as before until a model scorer is supplied.
        self.headline_scorer = headline_scorer or HeadlineSentimentScorer()
        self.intraday_window = intraday_window
        self.market_feed = market_feed
        self.news = news
        self.fundamentals = fundamentals
        self.macro = macro
        self.runtime = runtime or SwarmPaperTradingService()
        self.freshness = freshness or FreshnessValidator()
        self.regime_detector = regime_detector or MarketRegimeDetector()
        self.tick_reader = tick_reader
        self.sizer = sizer or PositionSizer()
        self.cache = IntelligenceDataCache()
        self.regime_observations = RegimeObservationStore()
        # Five directional specialists and two desks. The desks are gates (see
        # ``AtlasPolicy.gate_domains``): they can veto a cycle and are never counted as
        # votes. The technical agent stays last: its request is the root request the CIO
        # is handed, and it is the one whose freshness is the price feed alone.
        self.agents = (
            GeopoliticalAnalystAgent(),
            CommodityYieldAgent(),
            IndianEquitiesAgent(),
            USEquitiesAgent(),
            LiquidityDeskAgent(),
            RiskDeskAgent(),
            TechnicalQuantAgent(),
        )
        self._macro_current_at: datetime | None = None
        self._macro_current: dict[str, Decimal] = {}
        self._macro_previous: dict[str, Decimal] = {}

    def _analysis_request(self, instrument, now, metrics, max_age):
        request = AgentAnalysisRequest(instrument.symbol, instrument.market,
                                       instrument.asset_class, now, metrics, max_age)
        if self.bind_order_instruments:
            return InstrumentBoundAnalysisRequest(**vars(request), instrument=instrument)
        return request

    def _minute_candles(self, instrument: Instrument, now: datetime) -> tuple[Candle, ...]:
        """Use the latest 60 closed trading bars when the live feed has a warm cache."""
        recent = getattr(self.market_feed, "fetch_recent_ohlcv", None)
        if callable(recent):
            return tuple(recent(instrument, now, count=60))
        return self.market_feed.fetch_ohlcv(
            instrument, now - timedelta(minutes=60), now, "1m"
        )

    def _resolve_quantity(
        self,
        requested: int | None,
        plan: CapitalPlan,
        portfolio: PortfolioSnapshot,
        reference_price: Decimal,
    ) -> int:
        """An explicit quantity is honoured; None means size from risk and capital."""
        if requested is not None:
            return requested
        worst_entry = self.runtime.broker.friction_model.worst_case_execution_price(
            reference_price, Side.BUY
        )
        return self.sizer.quantity_from_plan(
            plan,
            portfolio,
            reference_price,
            worst_entry_price=worst_entry,
        )

    @staticmethod
    def _apply_conflict(quantity: int, conflict: Decimal) -> int:
        if quantity <= 0:
            return 0
        return max(1, quantity // 2) if conflict >= Decimal("0.40") else quantity

    def _protective_levels(
        self,
        plan: CapitalPlan,
        reference_price: Decimal,
    ) -> tuple[Decimal | None, Decimal | None]:
        """Price protection so bounded execution friction cannot invert reward geometry."""
        if reference_price <= 0:
            return None, None
        stop = reference_price * (Decimal(1) - plan.stop_loss_fraction)
        worst_entry = self.runtime.broker.friction_model.worst_case_execution_price(
            reference_price, Side.BUY
        )
        risk_distance = max(Decimal(0), worst_entry - stop)
        take_profit = worst_entry + risk_distance * plan.reward_risk_ratio
        return stop, take_profit

    def run(
        self,
        instrument: Instrument,
        now: datetime,
        plan: CapitalPlan,
        portfolio: PortfolioSnapshot,
        *,
        quantity: int | None = None,
        country: str,
        tenant_id: str = "default",
        country_exposure: dict[str, Decimal] | None = None,
        reference_price_override: Decimal | None = None,
    ) -> MarketAnalysisResult:
        candles = self._minute_candles(instrument, now)
        news = self.news.fetch(instrument.symbol, now)
        geopolitical = self.news.fetch("GEOPOLITICAL", now)
        fundamentals = self.fundamentals.fetch(instrument.symbol, now)
        macro = self.macro.fetch(MACRO_INDICATORS, now)

        last_price_at = candles[-1].timestamp if candles else None
        latest_news_at = max((item.published_at for item in news + geopolitical), default=None)
        required_macro = set(MACRO_CORE_INDICATORS)
        macro_at = macro.freshness_observed_at if required_macro <= macro.indicators.keys() else None
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
        analytics = summarize_performance(
            returns, tuple(curve), returns, periods=_one_minute_periods(instrument)
        )
        technical = self._technical_metrics(closes)
        market = self._market_context(instrument, candles, now)
        equity_news = self._recent_sentiment(news, now)
        geopolitical_sentiment = self._recent_sentiment(geopolitical, now)
        macro_metrics = self._macro_metrics(macro)

        common: dict[str, Decimal | str] = dict(fundamentals.metrics)
        common.update(technical)
        common.update(macro_metrics)
        common.update(market.metrics())
        common.update(self._desk_metrics(instrument.symbol, candles, effective_plan, portfolio, None))
        common["equity_news_sentiment"] = equity_news
        common["news_sentiment"] = geopolitical_sentiment
        common["conflict_risk"] = max(Decimal(0), -geopolitical_sentiment)
        common["sanctions_risk"] = max(Decimal(0), -geopolitical_sentiment / Decimal(2))

        requests = []
        for agent in self.agents:
            required = self._required_freshness(agent.agent_id, states)
            required_age = self._required_freshness_age(agent.agent_id, states)
            metrics = dict(common)
            metrics["freshness_multiplier"] = required
            metrics["freshness_diagnostic"] = self._freshness_diagnostic(agent.agent_id, states)
            requests.append(
                self._analysis_request(instrument, now, metrics, required_age)
            )
        evidence = tuple(agent.analyze(request) for agent, request in zip(self.agents, requests))
        conflict = self._conflict_ratio(evidence)
        root_request = requests[-1]
        reference_price = (
            reference_price_override
            if reference_price_override is not None and reference_price_override > 0
            else (closes[-1] if closes else Decimal(0))
        )
        requested_quantity = self._resolve_quantity(
            quantity, effective_plan, portfolio, reference_price
        )
        effective_quantity = self._apply_conflict(requested_quantity, conflict)
        stop, take_profit = self._protective_levels(effective_plan, reference_price)
        # The deterministic path builds no prompt, but the proof still records which
        # regime the decision was made in.
        evidence_context = EvidenceContext(regime=market.regime_evidence())
        execution = self.runtime.execute(
            root_request,
            evidence,
            effective_plan,
            portfolio,
            quantity=effective_quantity,
            reference_price=reference_price,
            stop_price=stop,
            take_profit_price=take_profit,
            country=country,
            country_exposure=country_exposure,
            tenant_id=tenant_id,
            evidence_context=evidence_context,
        )
        return MarketAnalysisResult(
            evidence,
            states,
            requested_quantity,
            effective_quantity,
            conflict,
            execution,
            regime,
            analytics,
            market.primary,
            # ``common`` and not the per-agent ``metrics``: the only difference is the
            # freshness multiplier each agent is weighted by, which belongs to the agent
            # rather than to the market, and is already reflected in its published
            # confidence. This is the vector the tick was judged on, once.
            features=dict(common),
        )

    async def run_async(
        self,
        instrument: Instrument,
        now: datetime,
        plan: CapitalPlan,
        portfolio: PortfolioSnapshot,
        *,
        quantity: int | None = None,
        country: str,
        tenant_id: str = "default",
        country_exposure: dict[str, Decimal] | None = None,
    ) -> MarketAnalysisResult:
        candles = self._minute_candles(instrument, now)
        news = self.news.fetch(instrument.symbol, now)
        geopolitical = self.news.fetch("GEOPOLITICAL", now)
        fundamentals = self.fundamentals.fetch(instrument.symbol, now)
        macro = self.macro.fetch(MACRO_INDICATORS, now)
        # Every headline is re-scored against this instrument before anything reads its
        # sentiment, so the specialists, the aggregate metrics and the consensus evidence
        # all see the same number and the same scorer label.
        scored_headlines = await self._score_headlines(instrument.symbol, news + geopolitical)
        equity_count = len(news)
        news = tuple(signal for signal, _ in scored_headlines[:equity_count])
        geopolitical = tuple(signal for signal, _ in scored_headlines[equity_count:])

        last_price_at = candles[-1].timestamp if candles else None
        latest_news_at = max((item.published_at for item in news + geopolitical), default=None)
        required_macro = set(MACRO_CORE_INDICATORS)
        macro_at = macro.freshness_observed_at if required_macro <= macro.indicators.keys() else None
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
        analytics = summarize_performance(
            returns, tuple(curve), returns, periods=_one_minute_periods(instrument)
        )
        technical = self._technical_metrics(closes)
        market = self._market_context(instrument, candles, now)
        common: dict[str, Decimal | str] = dict(fundamentals.metrics)
        common.update(technical)
        common.update(self._macro_metrics(macro))
        common.update(market.metrics())
        common["equity_news_sentiment"] = self._recent_sentiment(news, now)
        geo_sentiment = self._recent_sentiment(geopolitical, now)
        common["news_sentiment"] = geo_sentiment
        common["conflict_risk"] = max(Decimal(0), -geo_sentiment)
        common["sanctions_risk"] = max(Decimal(0), -geo_sentiment / Decimal(2))

        market_tick, market_data_veto = self._market_tick_status(instrument.symbol, now)
        if market_tick is not None:
            common["live_ltp"] = market_tick.ltp
            common["live_volume"] = market_tick.volume
            if market_tick.spread is not None:
                common["live_bid_ask_spread"] = market_tick.spread
        common.update(
            self._desk_metrics(instrument.symbol, candles, effective_plan, portfolio, market_tick)
        )

        requests = []
        for agent in self.agents:
            required = self._required_freshness(agent.agent_id, states)
            required_age = self._required_freshness_age(agent.agent_id, states)
            metrics = dict(common)
            metrics["freshness_multiplier"] = required
            metrics["freshness_diagnostic"] = self._freshness_diagnostic(agent.agent_id, states)
            requests.append(
                self._analysis_request(instrument, now, metrics, required_age)
            )
        evidence = tuple(agent.analyze(request) for agent, request in zip(self.agents, requests))
        conflict = self._conflict_ratio(evidence)
        root_request = requests[-1]
        reference_price = (
            market_tick.ltp
            if market_tick is not None and market_tick.ltp > 0
            else (closes[-1] if closes else Decimal(0))
        )
        requested_quantity = self._resolve_quantity(
            quantity, effective_plan, portfolio, reference_price
        )
        effective_quantity = self._apply_conflict(requested_quantity, conflict)
        stop, take_profit = self._protective_levels(effective_plan, reference_price)
        # The same evidence the specialists scored, summarized and bounded, so the LLM
        # consensus can reason about it instead of only re-weighting five numbers.
        evidence_context = self._evidence_context(
            candles, technical, scored_headlines, macro, fundamentals, states,
            timeframes=market.timeframe_evidence(), regime=market.regime_evidence(),
            lessons=self._approved_lessons(),
        )
        execution = await self.runtime.execute_async(
            root_request,
            evidence,
            effective_plan,
            portfolio,
            quantity=effective_quantity,
            reference_price=reference_price,
            stop_price=stop,
            take_profit_price=take_profit,
            country=country,
            market_tick=market_tick,
            preflight_veto_reason=market_data_veto,
            country_exposure=country_exposure,
            tenant_id=tenant_id,
            evidence_context=evidence_context,
        )
        return MarketAnalysisResult(
            evidence,
            states,
            requested_quantity,
            effective_quantity,
            conflict,
            execution,
            regime,
            analytics,
            market.primary,
            # ``common`` and not the per-agent ``metrics``: the only difference is the
            # freshness multiplier each agent is weighted by, which belongs to the agent
            # rather than to the market, and is already reflected in its published
            # confidence. This is the vector the tick was judged on, once.
            features=dict(common),
        )

    def _market_context(
        self, instrument: Instrument, candles: tuple[Candle, ...], now: datetime
    ) -> MarketContext:
        """Closed 15-minute and daily bars with their regime summaries; never raises."""
        history = self._intraday_history(instrument, candles, now)
        intraday_bars = aggregate(
            history, INTRADAY_TIMEFRAME_MINUTES, venue=venue_for(instrument.market), now=now
        )
        daily_bars = self._daily_history(instrument, now)
        intraday = classify(intraday_bars, timeframe=INTRADAY_TIMEFRAME)
        daily = classify(daily_bars, timeframe=DAILY_TIMEFRAME)
        context = MarketContext(
            intraday_bars, daily_bars, intraday, daily, primary_regime(daily, intraday)
        )
        self.regime_observations.record(instrument, now, len(candles), context)
        return context

    def _intraday_history(
        self, instrument: Instrument, candles: tuple[Candle, ...], now: datetime
    ) -> tuple[Candle, ...]:
        """The wider 1-minute window behind the 15-minute bars.

        The 60-minute fetch that feeds the specialists, the analytics and the plan is left
        untouched, so no sizing input moves. A feed that cannot serve the wider window
        leaves the 60-minute candles as the only intraday context.
        """
        if self.intraday_window <= timedelta(minutes=60):
            return candles
        try:
            wider = self.market_feed.fetch_ohlcv(
                instrument, now - self.intraday_window, now, "1m"
            )
        except _CONTEXT_FAILURES as exc:
            LOGGER.warning(
                "intraday history unavailable: symbol=%s reason=%s",
                instrument.symbol, f"{type(exc).__name__}: {exc}",
            )
            return candles
        return wider or candles

    def _daily_history(self, instrument: Instrument, now: datetime) -> tuple[Candle, ...]:
        """Closed daily bars from the history provider; a failing provider abstains."""
        if self.history is None:
            return ()
        try:
            return self.history.fetch(instrument, now)
        except _CONTEXT_FAILURES as exc:
            LOGGER.warning(
                "daily history unavailable: symbol=%s reason=%s",
                instrument.symbol, f"{type(exc).__name__}: {exc}",
            )
            return ()

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
    def _desk_metrics(
        symbol: str,
        candles: Sequence[Candle],
        plan: CapitalPlan,
        portfolio: PortfolioSnapshot,
        market_tick: LiveTick | None,
    ) -> dict[str, Decimal]:
        """Inputs for the liquidity and risk desks, derived from what is on hand.

        Nothing here is estimated. The traded-value median and the dead-tape count come
        from the closed one-minute bars of this cycle; the spread from the live two-sided
        quote, when there is one; every limit from the operator's (regime-adjusted) plan;
        every exposure from the ledger's own snapshot. An input that is absent is left out,
        and the desk that needed it abstains and names it.
        """
        metrics: dict[str, Decimal] = {}
        traded = sorted(c.close * c.volume for c in candles if c.close > 0 and c.volume >= 0)
        if traded:
            middle = len(traded) // 2
            metrics["median_minute_traded_value"] = (
                traded[middle] if len(traded) % 2 else (traded[middle - 1] + traded[middle]) / 2
            )
            dead = 0
            for candle in reversed(candles):
                if candle.volume > 0:
                    break
                dead += 1
            metrics["dead_tape_bars"] = Decimal(dead)
        metrics["intended_position_notional"] = plan.max_position_amount
        if market_tick is not None and market_tick.ltp > 0 and market_tick.spread is not None:
            metrics["live_bid_ask_spread_bps"] = (
                market_tick.spread / market_tick.ltp * Decimal(10000)
            )
        equity = portfolio.equity
        if equity > 0:
            peak = portfolio.peak_equity
            metrics["book_equity"] = equity
            metrics["book_gross_exposure_fraction"] = portfolio.gross_exposure / equity
            metrics["book_symbol_exposure_fraction"] = (
                portfolio.symbol_exposure.get(symbol, Decimal(0)) / equity
            )
            metrics["book_daily_pnl_fraction"] = portfolio.daily_total_pnl / equity
            metrics["book_drawdown_fraction"] = (
                max(Decimal(0), (peak - equity) / peak) if peak is not None and peak > 0 else Decimal(0)
            )
            metrics["plan_max_daily_loss_fraction"] = plan.max_daily_loss_fraction
            metrics["plan_max_drawdown_fraction"] = plan.max_drawdown_fraction
            metrics["plan_max_gross_exposure_fraction"] = plan.max_gross_exposure_fraction
            metrics["plan_max_position_fraction"] = plan.max_position_fraction
            metrics["plan_trading_allowed"] = Decimal(1 if plan.trading_allowed else 0)
        return metrics

    @staticmethod
    def _freshness_sources(agent_id: str) -> tuple[str, ...]:
        if agent_id == "geopolitical-analyst":
            return ("news",)
        if agent_id in {"liquidity-desk", "risk-desk"}:
            # The desks read the live quote, the closed bars and the book. The book is
            # always current; the quote and bars are the price feed.
            return ("price",)
        if agent_id == "commodity-yield":
            return ("macro",)
        if agent_id == "indian-equities":
            # This specialist scores valuation/balance-sheet fields plus equity news only.
            # Requiring macro data here silences a valid India vote when the unrelated macro
            # provider is unavailable.
            return ("news", "fundamentals")
        if agent_id == "us-equities":
            # US10Y is part of the score, so macro freshness remains a genuine dependency.
            return ("news", "macro", "fundamentals")
        return ("price",)

    @classmethod
    def _required_freshness(cls, agent_id: str, states: PipelineFreshness) -> Decimal:
        sources = cls._freshness_sources(agent_id)
        return min(getattr(states, source).confidence_multiplier for source in sources)

    @classmethod
    def _required_freshness_age(cls, agent_id: str, states: PipelineFreshness) -> int:
        """Age only the inputs this specialist actually consumes.

        A slow-moving macro series must not make fresh price/news evidence appear stale
        for unrelated specialists. Missing data is already represented by a zero
        freshness multiplier and NEUTRAL stance; its age remains zero rather than being
        fabricated.
        """
        sources = cls._freshness_sources(agent_id)
        return max((getattr(states, source).age_seconds or 0) for source in sources)

    @classmethod
    def _freshness_diagnostic(cls, agent_id: str, states: PipelineFreshness) -> str:
        parts = []
        for source in cls._freshness_sources(agent_id):
            result = getattr(states, source)
            age = "unknown" if result.age_seconds is None else str(result.age_seconds)
            parts.append(f"{source}={result.state.value}(age_seconds={age})")
        return ",".join(parts)

    def _approved_lessons(self) -> tuple[str, ...]:
        """Bounded operator-approved lessons, or nothing.

        The provider reads files a human approved, but a file is still untrusted input and
        a missing or malformed store is not a reason to lose a tick: any failure yields no
        lessons and one warning. ``EvidenceContext`` enforces the count and length bounds,
        so a provider that returns more than ``MAX_LESSONS`` is trimmed here rather than
        raising inside the evidence constructor.
        """
        if self.lessons_provider is None:
            return ()
        try:
            supplied = tuple(self.lessons_provider())
        except Exception:  # evidence is optional, the cadence is not
            LOGGER.warning("approved_lessons_unavailable", exc_info=True)
            return ()
        lessons: list[str] = []
        for item in supplied[:MAX_LESSONS]:
            if not isinstance(item, str):
                continue
            text = " ".join(item.split())[:MAX_LESSON_CHARS].strip()
            if text:
                lessons.append(text)
        return tuple(lessons)

    async def _score_headlines(
        self, subject: str, headlines: tuple[NewsSignal, ...]
    ) -> tuple[tuple[NewsSignal, HeadlineScore], ...]:
        """Re-score fetched headlines against this instrument, paired with their scores.

        The signals come back with the scored sentiment substituted, so downstream
        aggregation is unchanged. A scorer that returns the wrong shape, or fails in a way
        it did not already absorb, degrades every headline to the keyword score: news
        sentiment is evidence, and evidence is never a reason to lose a tick.
        """
        if not headlines:
            return ()
        try:
            scores = await self.headline_scorer.score(
                subject, [item.headline for item in headlines]
            )
        except _CONTEXT_FAILURES as exc:
            LOGGER.warning(
                "headline scoring unavailable: subject=%s reason=%s",
                subject, f"{type(exc).__name__}: {exc}",
            )
            scores = ()
        if len(scores) != len(headlines):
            scores = tuple(
                keyword_score(normalize_headline(item.headline)) for item in headlines
            )
        return tuple(
            (replace(signal, sentiment=score.sentiment), score)
            for signal, score in zip(headlines, scores)
        )

    @staticmethod
    def _evidence_context(
        candles: tuple[Candle, ...],
        technical: dict[str, Decimal],
        headlines: tuple[tuple[NewsSignal, HeadlineScore], ...],
        macro: MacroSnapshot,
        fundamentals: FundamentalSnapshot,
        states: PipelineFreshness,
        *,
        max_bars: int = MAX_EVIDENCE_BARS,
        max_headlines: int = MAX_EVIDENCE_HEADLINES,
        timeframes: tuple[tuple[str, tuple[EvidenceBar, ...]], ...] = (),
        regime: tuple[tuple[str, Decimal | str], ...] = (),
        lessons: tuple[str, ...] = (),
    ) -> EvidenceContext:
        """Pre-render the newest bars and headlines plus the metric maps for the prompt.

        Headline text is third-party data: it is collapsed to one line and cut to
        ``MAX_HEADLINE_CHARS`` here so the prompt renderer never has to sanitize. Each
        headline arrives paired with the score that was taken for it, so the rendered
        evidence says which scorer produced the number and why, and carries the alias when
        an operator's assertion - rather than the headline itself - attached it here.
        ``timeframes`` and ``regime`` arrive already bounded from ``MarketContext``.
        """
        bars = tuple(_evidence_bar(candle) for candle in candles[-max_bars:])
        newest = sorted(headlines, key=lambda item: item[0].published_at)[-max_headlines:]
        rendered_headlines = tuple(
            EvidenceHeadline(
                signal.subject,
                normalize_headline(signal.headline),
                score.sentiment,
                signal.published_at.isoformat(),
                signal.source,
                score.scorer,
                score.rationale,
                signal.matched_alias or "",
            )
            for signal, score in newest
        )

        def freshness(result: FreshnessResult) -> str:
            if result.age_seconds is None:
                return result.state.value
            return f"{result.state.value}(age_seconds={result.age_seconds})"

        return EvidenceContext(
            bars,
            tuple(sorted(technical.items())),
            rendered_headlines,
            tuple(sorted(macro.indicators.items())),
            macro.observed_at.isoformat() if macro.indicators else None,
            tuple(sorted(fundamentals.metrics.items())),
            fundamentals.observed_at.isoformat() if fundamentals.metrics else None,
            (
                ("price", freshness(states.price)),
                ("news", freshness(states.news)),
                ("macro", freshness(states.macro)),
                ("fundamentals", freshness(states.fundamentals)),
            ) + (
                (("macro_oldest_observed_at", macro.freshness_observed_at.isoformat()),)
                if macro.indicators and macro.oldest_observed_at is not None else ()
            ),
            timeframes=timeframes,
            regime=regime,
            lessons=lessons,
        )

    @staticmethod
    def _mean(values: tuple[Decimal, ...]) -> Decimal:
        return sum(values, Decimal(0)) / Decimal(len(values)) if values else Decimal(0)

    def _recent_sentiment(self, items: tuple[NewsSignal, ...], now: datetime) -> Decimal:
        floor = now - self.news_window
        recent = tuple(item.sentiment for item in items if item.published_at >= floor)
        return self._mean(recent)

    def _macro_metrics(self, snapshot: MacroSnapshot) -> dict[str, Decimal]:
        # The snapshot clock is the core's latest observation: the composite keeps it on the
        # part that supplied the core series, so the India parts, which move every bar during
        # the session, never make "previous" mean ten minutes ago.
        if self._macro_current_at is None:
            self._macro_current_at = snapshot.observed_at
            self._macro_current = dict(snapshot.indicators)
        elif snapshot.observed_at > self._macro_current_at:
            self._macro_previous = self._macro_current
            self._macro_current = dict(snapshot.indicators)
            self._macro_current_at = snapshot.observed_at

        values = snapshot.indicators
        previous = (
            self._macro_previous
            if snapshot.observed_at == self._macro_current_at
            else {}
        )

        def change(key: str) -> Decimal:
            current = values.get(key, Decimal(0))
            prior = previous.get(key)
            if prior is None or prior == 0:
                return Decimal(0)
            return (current - prior) / prior

        metrics = {
            "us10y": values.get("US10Y", Decimal(0)),
            "yield_change": change("US10Y"),
            "brent_change": change("BRENT"),
            "gold_change": change("GOLD"),
            "usd_broad_change": change("USD_BROAD"),
        }
        # India context is optional and never defaulted: an absent key tells the specialist
        # the series was not observed, which a zero could not.
        vix = values.get("INDIA_VIX")
        if vix is not None:
            metrics["india_vix"] = vix
            previous_close = values.get("INDIA_VIX_PREV_CLOSE")
            if previous_close is not None and previous_close > 0:
                metrics["india_vix_change"] = (vix - previous_close) / previous_close
        for indicator, metric in (("FII_NET_CRORE", "fii_net_crore"), ("DII_NET_CRORE", "dii_net_crore")):
            if indicator in values:
                metrics[metric] = values[indicator]
        return metrics

    @staticmethod
    def _technical_metrics(closes: tuple[Decimal, ...]) -> dict[str, Decimal]:
        bars = Decimal(len(closes))
        if len(closes) < 50:
            return {
                "sma_spread": Decimal(0),
                "rsi": Decimal(50),
                "momentum": Decimal(0),
                "price_history_bars": bars,
            }
        sma20 = sum(closes[-20:], Decimal(0)) / Decimal(20)
        sma50 = sum(closes[-50:], Decimal(0)) / Decimal(50)
        spread = (sma20 - sma50) / sma50 if sma50 else Decimal(0)
        momentum = (closes[-1] - closes[-11]) / closes[-11] if closes[-11] else Decimal(0)
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
        return {
            "sma_spread": spread,
            "rsi": rsi,
            "momentum": momentum,
            "price_history_bars": bars,
        }

    @staticmethod
    def _conflict_ratio(evidence: tuple[AgentEvidence, ...]) -> Decimal:
        positive = sum(
            item.confidence
            for item in evidence
            if item.stance in {Stance.BUY, Stance.STRONG_BUY}
        )
        negative = sum(
            item.confidence
            for item in evidence
            if item.stance in {Stance.SELL, Stance.STRONG_SELL, Stance.AVOID}
        )
        directional = positive + negative
        if directional <= 0:
            return Decimal(0)
        return min(positive, negative) / directional
