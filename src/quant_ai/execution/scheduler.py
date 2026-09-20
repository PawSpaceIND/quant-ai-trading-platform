from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from quant_ai.agents.contracts import Stance
from quant_ai.domain.models import Instrument, PortfolioSnapshot
from quant_ai.execution.briefing import FounderExecutionBrief
from quant_ai.execution.session import MarketCalendar, MarketState
from quant_ai.intelligence.pipeline import MarketAnalysisResult, SwarmMarketAnalysisPipeline
from quant_ai.intelligence.providers import MACRO_CORE_INDICATORS
from quant_ai.planning.capital import CapitalPlan


class AutonomousCadenceScheduler:
    def __init__(
        self,
        pipeline: SwarmMarketAnalysisPipeline,
        *,
        calendar: MarketCalendar | None = None,
        cadence: timedelta = timedelta(minutes=10),
    ) -> None:
        if cadence < timedelta(minutes=1):
            raise ValueError("cadence must be at least one minute")
        self.pipeline = pipeline
        self.calendar = calendar or MarketCalendar()
        self.cadence = cadence
        self.last_run_at: datetime | None = None
        # The full pipeline result behind the most recent brief (None off-hours), so the
        # daemon can journal the decision without widening the brief contract.
        self.last_result: MarketAnalysisResult | None = None

    def is_due(self, now: datetime) -> bool:
        return self.last_run_at is None or now - self.last_run_at >= self.cadence

    def run_tick(
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
    ) -> FounderExecutionBrief:
        state = self.calendar.state(instrument.market, now, exchange=instrument.exchange)
        self.last_run_at = now
        self.last_result = None
        if not instrument.tradable:
            return self._observation_brief(instrument, now, state)
        if state != MarketState.REGULAR_HOURS:
            return self._off_hours_brief(instrument, now, state)
        result = self.pipeline.run(
            instrument,
            now,
            plan,
            portfolio,
            quantity=quantity,
            country=country,
            tenant_id=tenant_id,
            country_exposure=country_exposure,
        )
        self.last_result = result
        return self._market_brief(instrument, now, state, result)

    async def run_tick_async(
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
    ) -> FounderExecutionBrief:
        state = self.calendar.state(instrument.market, now, exchange=instrument.exchange)
        self.last_run_at = now
        self.last_result = None
        if not instrument.tradable:
            return self._observation_brief(instrument, now, state)
        if state != MarketState.REGULAR_HOURS:
            return self._off_hours_brief(instrument, now, state)
        result = await self.pipeline.run_async(
            instrument, now, plan, portfolio, quantity=quantity, country=country,
            tenant_id=tenant_id, country_exposure=country_exposure,
        )
        self.last_result = result
        return self._market_brief(instrument, now, state, result)

    def _observation_brief(
        self, instrument: Instrument, now: datetime, state: MarketState
    ) -> FounderExecutionBrief:
        """What a watched, untradeable instrument is doing - no proposal, no order, no capital.

        An MCX metal is live for eight hours after the cash market shuts, and that evening
        session is where the move that gaps its ETF at the next open happens. Reading it needs
        none of the execution path and must not enter it, so the check sits ahead of the
        session branch: a ``tradable=False`` row never reaches the pipeline in any state. The
        prohibition is structural rather than conventional - ``instrument_identity_payload``
        and ``InstrumentBoundOrderIntent`` both refuse a non-tradable instrument outright.
        """
        candles = self.pipeline.market_feed.fetch_ohlcv(
            instrument, now - timedelta(minutes=60), now, "1m"
        )
        last = candles[-1].close if candles else None
        return FounderExecutionBrief(
            now,
            state,
            instrument.symbol,
            "OBSERVATION_ONLY",
            (f"last_price={last}" if last is not None else "last_price=unavailable",),
            "observation_only_instrument_not_tradable",
            (),
            (f"{instrument.exchange}_session={state.value}", f"bars={len(candles)}"),
        )

    def _off_hours_brief(
        self, instrument: Instrument, now: datetime, state: MarketState
    ) -> FounderExecutionBrief:
        provider_status: list[str] = []
        macro = self.pipeline.macro.fetch(MACRO_CORE_INDICATORS, now)
        news = self.pipeline.news.fetch("GEOPOLITICAL", now)
        provider_status.append(f"macro_indicators={len(macro.indicators)}")
        provider_status.append(f"geopolitical_items={len(news)}")
        sentiment = (
            sum((item.sentiment for item in news), Decimal(0)) / Decimal(len(news))
            if news
            else Decimal(0)
        )
        return FounderExecutionBrief(
            now,
            state,
            instrument.symbol,
            "OFF_HOURS_MACRO_GEO_SWEEP",
            (f"geopolitical_sentiment={sentiment}",),
            "equity_trade_generation_blocked_market_closed",
            (),
            tuple(provider_status),
        )

    @staticmethod
    def _market_brief(
        instrument: Instrument,
        now: datetime,
        state: MarketState,
        result: MarketAnalysisResult,
    ) -> FounderExecutionBrief:
        consensus = tuple(
            f"{item.agent_id}:{item.stance.value}:{item.confidence}"
            for item in result.evidence
        )
        fill = result.execution.fill
        orders = (fill.order_id,) if fill is not None else ()
        freshness = result.freshness
        provider_status = (
            f"price={freshness.price.state.value}",
            f"news={freshness.news.state.value}",
            f"macro={freshness.macro.state.value}",
            f"fundamentals={freshness.fundamentals.state.value}",
        )
        side = result.execution.proposal.side
        mode = "PAPER_TRADE" if fill is not None else "PRESERVE_CAPITAL"
        if side is None or all(item.stance == Stance.NEUTRAL for item in result.evidence):
            mode = "PRESERVE_CAPITAL"
        stress = result.execution.stress_verdict
        trace = result.execution.xai_trace
        return FounderExecutionBrief(
            now,
            state,
            instrument.symbol,
            mode,
            consensus,
            result.execution.risk_decision.reason,
            orders,
            provider_status,
            _regime_line(result),
            "PASS" if stress.passed else "STRESS_VETO",
            result.analytics.sharpe,
            result.analytics.sortino,
            trace.declared_rationales + (
                f"stress={stress.passed}:{stress.worst_scenario}",
                f"risk={result.execution.risk_decision.approved}:{result.execution.risk_decision.reason}",
            ),
            analytics_periods_per_year=result.analytics.periods_per_year,
        )


def _regime_line(result: MarketAnalysisResult) -> str:
    """Both regime readings for the operator digest, each named.

    ``result.regime`` is the exposure-scaling detector; ``regime_summary`` is the
    multi-timeframe label the decision was made under, which is what the journal, the
    dashboard breakdown and the proof all record. Showing one without saying which it is
    made the three surfaces look like they disagreed, so the digest names both.
    """
    sizing = result.regime.regime.value
    summary = getattr(result, "regime_summary", None)
    label = getattr(summary, "label", None)
    if not label or label == sizing:
        return sizing
    timeframe = getattr(summary, "timeframe", None)
    suffix = f"@{timeframe}" if timeframe else ""
    return f"{label}{suffix} (exposure:{sizing})"
