from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from quant_ai.agents.atlas import AtlasInvestmentAgent
from quant_ai.agents.contracts import AgentDomain, AgentEvidence, EvidenceContext, Stance
from quant_ai.domain.models import AssetClass, Instrument, Market, Side
from quant_ai.instruments.identity import immutable_instrument_snapshot
from quant_ai.risk.stops import orient_protective_levels


@dataclass(frozen=True)
class AgentAnalysisRequest:
    subject: str
    market: Market
    asset_class: AssetClass
    observed_at: datetime
    # Numeric inputs plus the deterministic ``regime_label`` string; every agent reads the
    # keys it needs with a typed default and never iterates the whole map.
    metrics: dict[str, Decimal | str]
    source_freshness_seconds: int = 0


@dataclass(frozen=True)
class InstrumentBoundAnalysisRequest(AgentAnalysisRequest):
    """Opt-in analysis of an exact snapshot; legacy cash requests are unchanged."""
    instrument: Instrument | None = None

    def __post_init__(self) -> None:
        instrument = immutable_instrument_snapshot(self.instrument)
        if (self.subject, self.market, self.asset_class) != (
            instrument.symbol, instrument.market, instrument.asset_class
        ):
            raise ValueError("analysis_request_instrument_identity_mismatch")
        object.__setattr__(self, "instrument", instrument)


# Valuation-driven agents only have an opinion on instruments that carry a balance
# sheet; a metal, a rupee pair or a crude contract has no P/E and must not be scored
# as if it had a bad one.
EQUITY_LIKE = frozenset({AssetClass.EQUITY, AssetClass.ETF, AssetClass.INDEX})


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
        freshness = request.metrics.get("freshness_multiplier", Decimal(1))
        adjusted_confidence = max(Decimal(0), min(Decimal(1), confidence * freshness))
        stance = self._stance(clamped) if freshness > Decimal("0.25") else Stance.NEUTRAL
        reasons = [rationale]
        if freshness < 1:
            reasons.append(f"freshness_penalty={freshness}")
            diagnostic = request.metrics.get("freshness_diagnostic")
            if isinstance(diagnostic, str) and diagnostic:
                reasons.append(f"freshness_sources={diagnostic}")
        if freshness <= Decimal("0.25"):
            reasons.append("capital_preservation_stale_or_missing_data")
        return AgentEvidence(
            self.agent_id,
            self.domain,
            request.subject,
            stance,
            adjusted_confidence,
            clamped * Decimal("0.04"),
            abs(clamped) * Decimal("0.03"),
            tuple(reasons),
            request.observed_at,
            request.source_freshness_seconds,
        )


class GeopoliticalAnalystAgent(SwarmAgent):
    agent_id = "geopolitical-analyst"
    domain = AgentDomain.NEWS

    def analyze(self, request: AgentAnalysisRequest) -> AgentEvidence:
        sentiment = request.metrics.get("news_sentiment", Decimal(0))
        sanctions = request.metrics.get("sanctions_risk", Decimal(0))
        conflict = request.metrics.get("conflict_risk", Decimal(0))
        score = sentiment - (sanctions + conflict) / Decimal(2)
        if sentiment <= Decimal("-0.50") or conflict >= Decimal("0.65"):
            score = min(score, Decimal("-0.70"))
        return self._evidence(request, score, Decimal("0.76"), "conflict_trade_and_sanctions_sentiment")


class CommodityYieldAgent(SwarmAgent):
    agent_id = "commodity-yield"
    domain = AgentDomain.MACRO

    def analyze(self, request: AgentAnalysisRequest) -> AgentEvidence:
        crude = request.metrics.get("brent_change", Decimal(0))
        gold = request.metrics.get("gold_change", Decimal(0))
        yields = request.metrics.get("yield_change", Decimal(0))
        dxy = request.metrics.get("dxy_change", Decimal(0))
        inflation_headwind = max(Decimal(0), crude) + max(Decimal(0), yields)
        # Sign corrected: a rising gold price is a risk-off bid, not support for the equity
        # being analysed. Capital rotating into the metal is capital leaving equity risk, so
        # gold strength is a headwind on a long-only cash book and gold weakness is the mild
        # risk-on tailwind. The term previously added a flight to safety to the score, which
        # read every risk-off day as a reason to buy the stock.
        risk_off_bid = gold / Decimal(2)
        score = -risk_off_bid - inflation_headwind - max(Decimal(0), dxy) / Decimal(2)
        return self._evidence(request, score, Decimal("0.74"), "crude_gold_yield_and_dollar_regime")


class IndianEquitiesAgent(SwarmAgent):
    agent_id = "indian-equities"
    domain = AgentDomain.COUNTRY

    def analyze(self, request: AgentAnalysisRequest) -> AgentEvidence:
        if request.market != Market.INDIA:
            return self._evidence(request, Decimal(0), Decimal("0.30"), "non_india_market")
        if request.asset_class not in EQUITY_LIKE:
            return self._evidence(request, Decimal(0), Decimal("0.30"), "non_equity_instrument")
        pe = request.metrics.get("pe", Decimal(0))
        debt = request.metrics.get("debt_equity", Decimal(0))
        margin = request.metrics.get("operating_margin", Decimal(0))
        fcf = request.metrics.get("fcf_yield", Decimal(0))
        news = request.metrics.get("equity_news_sentiment", Decimal(0))
        score = Decimal(0)
        score += Decimal("0.30") if 0 < pe <= 30 else Decimal("-0.15")
        score += Decimal("0.20") if debt <= Decimal("0.75") else Decimal("-0.20")
        score += Decimal("0.25") if margin >= Decimal("0.15") else Decimal("-0.10")
        score += Decimal("0.15") if fcf >= Decimal("0.025") else Decimal("-0.05")
        score += news * Decimal("0.30")
        return self._evidence(request, score, Decimal("0.80"), "india_valuation_balance_sheet_margin_and_news")


class USEquitiesAgent(SwarmAgent):
    agent_id = "us-equities"
    domain = AgentDomain.PORTFOLIO

    def analyze(self, request: AgentAnalysisRequest) -> AgentEvidence:
        if request.market != Market.USA:
            return self._evidence(request, Decimal(0), Decimal("0.30"), "non_us_market")
        if request.asset_class not in EQUITY_LIKE:
            return self._evidence(request, Decimal(0), Decimal("0.30"), "non_equity_instrument")
        pe = request.metrics.get("pe", Decimal(0))
        margin = request.metrics.get("operating_margin", Decimal(0))
        fcf = request.metrics.get("fcf_yield", Decimal(0))
        us10y = request.metrics.get("us10y", Decimal(0))
        news = request.metrics.get("equity_news_sentiment", Decimal(0))
        score = Decimal(0)
        score += Decimal("0.25") if 0 < pe <= 35 else Decimal("-0.20")
        score += Decimal("0.30") if margin >= Decimal("0.20") else Decimal("-0.10")
        score += Decimal("0.15") if fcf >= Decimal("0.025") else Decimal("-0.05")
        score += Decimal("0.15") if us10y <= Decimal("4.5") else Decimal("-0.20")
        score += news * Decimal("0.30")
        return self._evidence(request, score, Decimal("0.80"), "us_tech_valuation_margin_fcf_and_rates")


class TechnicalQuantAgent(SwarmAgent):
    agent_id = "technical-quant-mas"
    domain = AgentDomain.TECHNICAL

    def analyze(self, request: AgentAnalysisRequest) -> AgentEvidence:
        if request.metrics.get("price_history_bars", Decimal(50)) < Decimal(50):
            return self._evidence(request, Decimal(0), Decimal("0.30"), "insufficient_price_history")
        spread = request.metrics.get("sma_spread", Decimal(0))
        rsi = request.metrics.get("rsi", Decimal(50))
        momentum = request.metrics.get("momentum", Decimal(0))
        score = spread * Decimal(4) + momentum * Decimal(3)
        if rsi >= 75:
            score -= Decimal("0.45")
        elif rsi <= 25:
            score += Decimal("0.35")
        elif Decimal(45) <= rsi <= Decimal(65):
            score += Decimal("0.10") if momentum > 0 else Decimal(0)
        return self._evidence(request, score, Decimal("0.84"), "sma20_sma50_rsi_and_momentum")


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
    provenance: dict | None = None


@dataclass(frozen=True)
class InstrumentBoundTradeProposal(TradeProposal):
    """CIO proposal carrying the same immutable decision-time instrument snapshot."""
    instrument: Instrument | None = None

    def __post_init__(self) -> None:
        instrument = immutable_instrument_snapshot(self.instrument)
        if (self.symbol, self.market, self.asset_class) != (
            instrument.symbol, instrument.market, instrument.asset_class
        ):
            raise ValueError("proposal_instrument_identity_mismatch")
        object.__setattr__(self, "instrument", instrument)


class AtlasCIOAgent:
    """CIO synthesizer. It proposes trades but has no execution capability."""

    def __init__(self, atlas: AtlasInvestmentAgent | None = None) -> None:
        self.atlas = atlas or AtlasInvestmentAgent()

    async def propose_async(
        self,
        request: AgentAnalysisRequest,
        evidence: tuple[AgentEvidence, ...],
        *,
        quantity: int,
        reference_price: Decimal,
        stop_price: Decimal | None,
        take_profit_price: Decimal | None,
        country: str,
        market_tick: object | None = None,
        evidence_context: EvidenceContext | None = None,
    ) -> TradeProposal:
        decision = await self.atlas.decide_with_llm(
            request.subject, evidence, request.observed_at, market_tick=market_tick,
            evidence_context=evidence_context,
        )
        return self._proposal_from_decision(
            request, decision, quantity=quantity, reference_price=reference_price,
            stop_price=stop_price, take_profit_price=take_profit_price, country=country,
        )

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
        evidence_context: EvidenceContext | None = None,
    ) -> TradeProposal:
        decision = self.atlas.decide(
            request.subject, evidence, request.observed_at, evidence_context=evidence_context
        )
        return self._proposal_from_decision(
            request, decision, quantity=quantity, reference_price=reference_price,
            stop_price=stop_price, take_profit_price=take_profit_price, country=country,
        )

    @staticmethod
    def _proposal_from_decision(
        request: AgentAnalysisRequest,
        decision: object,
        *,
        quantity: int,
        reference_price: Decimal,
        stop_price: Decimal | None,
        take_profit_price: Decimal | None,
        country: str,
    ) -> TradeProposal:
        side = None
        action = decision.action
        if action in {Stance.BUY, Stance.STRONG_BUY}:
            side = Side.BUY
        elif action in {Stance.SELL, Stance.STRONG_SELL}:
            side = Side.SELL
        # The pipeline computes levels before the side is known; orient them now.
        stop_price, take_profit_price = orient_protective_levels(
            side, reference_price, stop_price, take_profit_price
        )
        proposal = TradeProposal(
            decision.cycle_id, request.subject, request.market, country,
            request.asset_class, side, quantity, reference_price, stop_price,
            take_profit_price, decision.confidence,
            decision.expected_return, decision.expected_risk,
            decision.rationale,
            getattr(decision, "provenance", None),
        )
        instrument = getattr(request, "instrument", None)
        if instrument is not None:
            return InstrumentBoundTradeProposal(**vars(proposal), instrument=instrument)
        return proposal
