from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from quant_ai.agents.contracts import Stance
from quant_ai.domain.models import Instrument, PortfolioSnapshot
from quant_ai.execution.session import MarketCalendar, MarketState
from quant_ai.intelligence.pipeline import MarketAnalysisResult, SwarmMarketAnalysisPipeline
from quant_ai.planning.capital import CapitalPlan


@dataclass(frozen=True)
class FounderExecutionBrief:
    generated_at: datetime
    market_state: MarketState
    subject: str
    mode: str
    swarm_consensus: tuple[str, ...]
    risk_decision: str
    paper_order_ids: tuple[str, ...]
    provider_status: tuple[str, ...]

    def to_json(self) -> str:
        payload = asdict(self)
        payload["generated_at"] = self.generated_at.isoformat()
        payload["market_state"] = self.market_state.value
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


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

    def is_due(self, now: datetime) -> bool:
        return self.last_run_at is None or now - self.last_run_at >= self.cadence

    def run_tick(
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
    ) -> FounderExecutionBrief:
        state = self.calendar.state(instrument.market, now)
        self.last_run_at = now
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
        return self._market_brief(instrument, now, state, result)

    def _off_hours_brief(
        self, instrument: Instrument, now: datetime, state: MarketState
    ) -> FounderExecutionBrief:
        provider_status: list[str] = []
        macro = self.pipeline.macro.fetch(
            ("US10Y", "INDIA10Y", "BRENT", "GOLD", "DXY"), now
        )
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
        return FounderExecutionBrief(
            now,
            state,
            instrument.symbol,
            mode,
            consensus,
            result.execution.risk_decision.reason,
            orders,
            provider_status,
        )
