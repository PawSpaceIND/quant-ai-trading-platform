from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from quant_ai.agents.atlas import AtlasInvestmentAgent
from quant_ai.agents.contracts import AgentDomain, AgentEvidence, Stance
from quant_ai.domain.models import AssetClass, Market, Side


@dataclass(frozen=True)
class AgentAnalysisRequest:
    subject: str
    market: Market
    asset_class: AssetClass
    observed_at: datetime
    metrics: dict[str, Decimal]
    source_freshness_seconds: int = 0


class SwarmAgent(ABC):
    agent_id: str
    domain: AgentDomain

    @abstractmethod
    def analyze(self, request: AgentAnalysisRequest) -> AgentEvidence:
        raise NotImplementedError

    @staticmethod
    def _stance(score: Decimal) -> Stance:
        if score >= Decimal("0.60"):
            return Stance.STRONG_BUY
        if score >= Decimal("0.15"):
            return Stance.BUY
        if score <= Decimal("-0.60"):
            return Stance.STRONG_SELL
        if score <= Decimal("-0.15"):
            return Stance.SELL
        return Stance.NEUTRAL

    def _evidence(
        self, request: AgentAnalysisRequest, score: Decimal, confidence: Decimal, rationale: str
    ) -> AgentEvidence:
        clamped = max(Decimal(-1), min(Decimal(1), score))
        return AgentEvidence(
            self.agent_id,
            self.domain,
            request.subject,
            self._stance(clamped),
            max(Decimal(0), min(Decimal(1), confidence)),
            clamped * Decimal("0.04"),
            abs(clamped) * Decimal("0.03"),
            (rationale,),
            request.observed_at,
            request.source_freshness_seconds,
        )


class GeopoliticalAnalystAgent(SwarmAgent):
    agent_id = "geopolitical-analyst"
    domain = AgentDomain.NEWS

    def analyze(self, request: AgentAnalysisRequest) -> AgentEvidence:
        sentiment = request.metrics.get("news_sentiment", Decimal(0))
        geopolitical_risk = request.metrics.get("geopolitical_risk", Decimal(0))
        return self._evidence(
            request, sentiment - geopolitical_risk, Decimal("0.70"), "news_and_geopolitical_risk"
        )


class CommodityYieldAgent(SwarmAgent):
    agent_id = "commodity-yield"
    domain = AgentDomain.MACRO

    def analyze(self, request: AgentAnalysisRequest) -> AgentEvidence:
        commodity = request.metrics.get("commodity_momentum", Decimal(0))
        yield_pressure = request.metrics.get("yield_pressure", Decimal(0))
        return self._evidence(
            request, commodity - yield_pressure, Decimal("0.68"), "commodities_and_bond_yields"
        )


class IndianEquitiesAgent(SwarmAgent):
    agent_id = "indian-equities"
    domain = AgentDomain.COUNTRY

    def analyze(self, request: AgentAnalysisRequest) -> AgentEvidence:
        market_score = request.metrics.get("india_equity_score", Decimal(0))
        if request.market != Market.INDIA:
            market_score *= Decimal("0.25")
        return self._evidence(request, market_score, Decimal("0.72"), "nse_and_india_equity_context")


class USEquitiesAgent(SwarmAgent):
    agent_id = "us-equities"
    domain = AgentDomain.PORTFOLIO

    def analyze(self, request: AgentAnalysisRequest) -> AgentEvidence:
        market_score = request.metrics.get("us_equity_score", Decimal(0))
        if request.market != Market.USA:
            market_score *= Decimal("0.25")
        return self._evidence(request, market_score, Decimal("0.72"), "us_equity_and_sec_context")


class TechnicalQuantAgent(SwarmAgent):
    agent_id = "technical-quant-mas"
    domain = AgentDomain.TECHNICAL

    def analyze(self, request: AgentAnalysisRequest) -> AgentEvidence:
        momentum = request.metrics.get("momentum", Decimal(0))
        trend = request.metrics.get("trend", Decimal(0))
        score = (momentum + trend) / Decimal(2)
        return self._evidence(request, score, Decimal("0.78"), "price_action_and_momentum")


@dataclass(frozen=True)
class TradeProposal:
    decision_id: str
    symbol: str
    market: Market
    country: str
    asset_class: AssetClass
    side: Side | None
    quantity: int
    reference_price: Decimal
    stop_price: Decimal | None
    take_profit_price: Decimal | None
    confidence: Decimal
    expected_return: Decimal
    expected_risk: Decimal
    rationale: tuple[str, ...]


class AtlasCIOAgent:
    """CIO synthesizer. It proposes trades but has no execution capability."""

    def __init__(self, atlas: AtlasInvestmentAgent | None = None) -> None:
        self.atlas = atlas or AtlasInvestmentAgent()

    def propose(
        self,
        request: AgentAnalysisRequest,
        evidence: tuple[AgentEvidence, ...],
        *,
        quantity: int,
        reference_price: Decimal,
        stop_price: Decimal | None,
        take_profit_price: Decimal | None,
        country: str,
    ) -> TradeProposal:
        decision = self.atlas.decide(request.subject, evidence, request.observed_at)
        side = None
        if decision.action in {Stance.BUY, Stance.STRONG_BUY}:
            side = Side.BUY
        elif decision.action in {Stance.SELL, Stance.STRONG_SELL}:
            side = Side.SELL
        return TradeProposal(
            decision.cycle_id,
            request.subject,
            request.market,
            country,
            request.asset_class,
            side,
            quantity,
            reference_price,
            stop_price,
            take_profit_price,
            decision.confidence,
            decision.expected_return,
            decision.expected_risk,
            decision.rationale,
        )
