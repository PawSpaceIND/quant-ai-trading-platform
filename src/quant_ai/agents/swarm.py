from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
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
EQUITY_LIKE = frozenset({AssetClass.EQUITY})

# The valuation ratios each specialist scores, in rationale order. The fundamentals
# provider returns only the ratios its source carries (most NSE listings publish no free
# cash flow; a loss-maker has no trailing P/E), so a specialist scores the ratios it was
# given, names the ones it was not, scales its confidence by coverage and abstains below
# two: one ratio is a hint, not a valuation. An absent ratio is never scored as a bad one.
INDIA_VALUATION_RATIOS = ("pe", "debt_equity", "operating_margin", "fcf_yield")
US_VALUATION_RATIOS = ("pe", "operating_margin", "fcf_yield")
MIN_VALUATION_RATIOS = 2
VALUATION_CONFIDENCE = Decimal("0.80")


def _valuation_ratios(
    metrics: Mapping[str, Decimal | str], names: tuple[str, ...]
) -> tuple[dict[str, Decimal], Decimal, str]:
    """The ratios present, the confidence their coverage earns and the rationale note.

    Full coverage earns the full confidence and an empty note, so a complete valuation
    reads exactly as it always has. Partial coverage earns ``0.80 * seen / needed`` and a
    note naming the count and the missing ratios.
    """
    observed = {
        name: value
        for name in names
        if isinstance(value := metrics.get(name), Decimal)
    }
    if len(observed) == len(names):
        return observed, VALUATION_CONFIDENCE, ""
    missing = ",".join(name for name in names if name not in observed)
    confidence = VALUATION_CONFIDENCE * Decimal(len(observed)) / Decimal(len(names))
    return observed, confidence, f";fundamentals={len(observed)}/{len(names)}:missing={missing}"

# India VIX regime, from the index's own history since 2010: the median sits near 15, the
# top quintile begins around 20, and readings above 25 belong to shocks (March 2020, the
# June 2024 count). A stressed tape is a headwind for a new long in a single name; a calm
# tape earns nothing, because calm is the normal state and this specialist's edge is the
# balance sheet, not the regime. A same-day jump of 15% or more is fear arriving, whatever
# the level it starts from.
INDIA_VIX_ELEVATED = Decimal(20)
INDIA_VIX_STRESSED = Decimal(25)
INDIA_VIX_SPIKE = Decimal("0.15")
# Foreign institutional net flow in the cash market, rupees crore per day. Ordinary days
# run in the low thousands either way; 2,000 crore is a day the tape notices. Selling
# weighs more than buying: foreign outflows have led every sharp Indian drawdown, while
# inflows arrive into strength that the price already shows.
FII_FLOW_NOTABLE_CRORE = Decimal(2000)


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
        reasons = [rationale, *self._freshness_notes(request, freshness)]
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

    @staticmethod
    def _freshness_notes(request: AgentAnalysisRequest, freshness: Decimal) -> list[str]:
        notes: list[str] = []
        if freshness < 1:
            notes.append(f"freshness_penalty={freshness}")
            diagnostic = request.metrics.get("freshness_diagnostic")
            if isinstance(diagnostic, str) and diagnostic:
                notes.append(f"freshness_sources={diagnostic}")
        if freshness <= Decimal("0.25"):
            notes.append("capital_preservation_stale_or_missing_data")
        return notes

    def _gate(
        self, request: AgentAnalysisRequest, stance: Stance, confidence: Decimal, rationale: str
    ) -> AgentEvidence:
        """Evidence whose stance the desk chose itself, under the same freshness rule.

        A desk verdict on stale or missing inputs is worth nothing either way, so it
        collapses to the same silent NEUTRAL the scoring agents produce, and the rationale
        says so. A gate carries no return or risk estimate: it is not a forecast.
        """
        freshness = request.metrics.get("freshness_multiplier", Decimal(1))
        if freshness <= Decimal("0.25"):
            return self._evidence(request, Decimal(0), Decimal(0), rationale)
        adjusted_confidence = max(Decimal(0), min(Decimal(1), confidence * freshness))
        return AgentEvidence(
            self.agent_id,
            self.domain,
            request.subject,
            stance,
            adjusted_confidence,
            Decimal(0),
            Decimal(0),
            (rationale, *self._freshness_notes(request, freshness)),
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
        if request.asset_class is not AssetClass.EQUITY:
            return self._evidence(request, Decimal(0), Decimal(0), "non_equity_macro_model")
        crude = request.metrics.get("brent_change", Decimal(0))
        gold = request.metrics.get("gold_change", Decimal(0))
        yields = request.metrics.get("yield_change", Decimal(0))
        usd_broad = request.metrics.get("usd_broad_change", Decimal(0))
        inflation_headwind = max(Decimal(0), crude) + max(Decimal(0), yields)
        # Sign corrected: a rising gold price is a risk-off bid, not support for the equity
        # being analysed. Capital rotating into the metal is capital leaving equity risk, so
        # gold strength is a headwind on a long-only cash book and gold weakness is the mild
        # risk-on tailwind. The term previously added a flight to safety to the score, which
        # read every risk-off day as a reason to buy the stock.
        risk_off_bid = gold / Decimal(2)
        score = -risk_off_bid - inflation_headwind - max(Decimal(0), usd_broad) / Decimal(2)
        return self._evidence(request, score, Decimal("0.74"), "crude_gold_yield_and_broad_dollar_regime")


class IndianEquitiesAgent(SwarmAgent):
    agent_id = "indian-equities"
    domain = AgentDomain.COUNTRY

    def analyze(self, request: AgentAnalysisRequest) -> AgentEvidence:
        if request.market != Market.INDIA:
            return self._evidence(request, Decimal(0), Decimal(0), "non_india_market")
        if request.asset_class not in EQUITY_LIKE:
            return self._evidence(request, Decimal(0), Decimal(0), "non_equity_instrument")
        ratios, confidence, coverage = _valuation_ratios(request.metrics, INDIA_VALUATION_RATIOS)
        if len(ratios) < MIN_VALUATION_RATIOS:
            return self._evidence(
                request, Decimal(0), Decimal(0), "india_fundamentals_insufficient" + coverage
            )
        news = request.metrics.get("equity_news_sentiment", Decimal(0))
        score = Decimal(0)
        if (pe := ratios.get("pe")) is not None:
            score += Decimal("0.30") if 0 < pe <= 30 else Decimal("-0.15")
        if (debt := ratios.get("debt_equity")) is not None:
            score += Decimal("0.20") if debt <= Decimal("0.75") else Decimal("-0.20")
        if (margin := ratios.get("operating_margin")) is not None:
            score += Decimal("0.25") if margin >= Decimal("0.15") else Decimal("-0.10")
        if (fcf := ratios.get("fcf_yield")) is not None:
            score += Decimal("0.15") if fcf >= Decimal("0.025") else Decimal("-0.05")
        score += news * Decimal("0.30")
        rationale = "india_valuation_balance_sheet_margin_and_news" + coverage
        if request.asset_class is AssetClass.EQUITY:
            # The regime and flow terms read the Indian equity tape, so they apply to single
            # names only: the metal ETFs in the same book move with gold and silver, which a
            # stressed equity tape tends to lift. Both are optional inputs, present only when
            # observed; nothing here treats an absent series as a calm one.
            adjustment, notes = self._india_tape(request.metrics)
            score += adjustment
            rationale += notes
        return self._evidence(request, score, confidence, rationale)

    @staticmethod
    def _india_tape(metrics: Mapping[str, Decimal]) -> tuple[Decimal, str]:
        adjustment = Decimal(0)
        notes = ""
        vix = metrics.get("india_vix")
        if vix is not None:
            regime = "calm"
            if vix >= INDIA_VIX_STRESSED:
                adjustment -= Decimal("0.35")
                regime = "stressed"
            elif vix >= INDIA_VIX_ELEVATED:
                adjustment -= Decimal("0.20")
                regime = "elevated"
            notes += f";india_vix={vix}:{regime}"
            if metrics.get("india_vix_change", Decimal(0)) >= INDIA_VIX_SPIKE:
                adjustment -= Decimal("0.15")
                notes += ";india_vix_spike"
        fii = metrics.get("fii_net_crore")
        if fii is not None:
            flow = "quiet"
            if fii >= FII_FLOW_NOTABLE_CRORE:
                adjustment += Decimal("0.10")
                flow = "inflow"
            elif fii <= -FII_FLOW_NOTABLE_CRORE:
                adjustment -= Decimal("0.15")
                flow = "outflow"
            notes += f";fii_net_crore={fii}:{flow}"
        return adjustment, notes


class USEquitiesAgent(SwarmAgent):
    agent_id = "us-equities"
    domain = AgentDomain.PORTFOLIO

    def analyze(self, request: AgentAnalysisRequest) -> AgentEvidence:
        if request.market != Market.USA:
            return self._evidence(request, Decimal(0), Decimal(0), "non_us_market")
        if request.asset_class not in EQUITY_LIKE:
            return self._evidence(request, Decimal(0), Decimal(0), "non_equity_instrument")
        ratios, confidence, coverage = _valuation_ratios(request.metrics, US_VALUATION_RATIOS)
        if len(ratios) < MIN_VALUATION_RATIOS:
            return self._evidence(
                request, Decimal(0), Decimal(0), "us_fundamentals_insufficient" + coverage
            )
        us10y = request.metrics.get("us10y", Decimal(0))
        news = request.metrics.get("equity_news_sentiment", Decimal(0))
        score = Decimal(0)
        if (pe := ratios.get("pe")) is not None:
            score += Decimal("0.25") if 0 < pe <= 35 else Decimal("-0.20")
        if (margin := ratios.get("operating_margin")) is not None:
            score += Decimal("0.30") if margin >= Decimal("0.20") else Decimal("-0.10")
        if (fcf := ratios.get("fcf_yield")) is not None:
            score += Decimal("0.15") if fcf >= Decimal("0.025") else Decimal("-0.05")
        score += Decimal("0.15") if us10y <= Decimal("4.5") else Decimal("-0.20")
        score += news * Decimal("0.30")
        return self._evidence(
            request, score, confidence, "us_tech_valuation_margin_fcf_and_rates" + coverage
        )


class TechnicalQuantAgent(SwarmAgent):
    agent_id = "technical-quant-mas"
    domain = AgentDomain.TECHNICAL

    def analyze(self, request: AgentAnalysisRequest) -> AgentEvidence:
        if request.metrics.get("price_history_bars", Decimal(50)) < Decimal(50):
            return self._evidence(request, Decimal(0), Decimal(0), "insufficient_price_history")
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


class ETFValueReferenceAgent(SwarmAgent):
    """Observable fund-value context only; never a directional vote or an order veto."""
    agent_id = "etf-value-reference"
    domain = AgentDomain.RISK

    def analyze(self, request: AgentAnalysisRequest) -> AgentEvidence:
        if request.asset_class is not AssetClass.ETF:
            reason = "non_etf_instrument"
        else:
            reason = request.metrics.get("etf_reference_status", "etf_reference_unconfigured")
        return self._evidence(request, Decimal(0), Decimal(0), str(reason))


# ---------------------------------------------------------------------------------------
# Desk specialists: gates, not voters.
#
# The liquidity desk and the risk desk hold no view on direction. Their question is whether
# the book should be allowed to act at all on this tick: is the quote tradable, and does the
# book have the room. They report LIQUIDITY / RISK evidence, and Atlas treats those domains
# as gates - an AVOID vetoes the cycle; anything else is recorded and kept out of both the
# directional mean and the coverage floor. A desk can therefore never manufacture a consensus
# and never dilute one, which is what let three silenced specialists pin the pilot at 0.32.
#
# Every threshold is either the operator's own capital plan, carried in the request metrics,
# or an explicit policy below. Nothing is invented: a desk that cannot observe its inputs
# abstains (NEUTRAL, zero confidence) and says which input was missing.
# ---------------------------------------------------------------------------------------

def _metric(request: AgentAnalysisRequest, key: str) -> Decimal | None:
    value = request.metrics.get(key)
    return value if isinstance(value, Decimal) else None


def _quantized(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"))


@dataclass(frozen=True)
class LiquidityDeskPolicy:
    """What the liquidity desk refuses to trade into.

    ``max_spread_bps``: the quoted bid/ask spread, in basis points of the last price, above
    which an entry is refused. NSE large caps quote inside 5 bps for most of the session;
    30 is a quote that has come apart (pre-open, halt, thin book), not one that is wide.

    ``max_participation``: the plan's maximum position notional as a share of the median
    one-minute traded value in the analysis window. An order that would be more than a
    quarter of a typical minute's turnover moves the price it is filled at.

    ``dead_tape_bars``: consecutive closed one-minute bars with no volume at the end of the
    window. Five silent minutes on a listed large cap means the tape has stopped, whatever
    the last quote says.
    """

    max_spread_bps: Decimal = Decimal(30)
    max_participation: Decimal = Decimal("0.25")
    dead_tape_bars: int = 5

    def __post_init__(self) -> None:
        if self.max_spread_bps <= 0 or self.max_participation <= 0 or self.dead_tape_bars < 1:
            raise ValueError("liquidity_desk_policy_invalid")


class LiquidityDeskAgent(SwarmAgent):
    agent_id = "liquidity-desk"
    domain = AgentDomain.LIQUIDITY

    def __init__(self, policy: LiquidityDeskPolicy | None = None) -> None:
        self.policy = policy or LiquidityDeskPolicy()

    def analyze(self, request: AgentAnalysisRequest) -> AgentEvidence:
        spread_bps = _metric(request, "live_bid_ask_spread_bps")
        if spread_bps is None:
            # No two-sided live quote on this tick: the desk cannot judge, so it does not.
            return self._evidence(request, Decimal(0), Decimal(0), "liquidity_unobserved:no_live_quote")
        median_value = _metric(request, "median_minute_traded_value") or Decimal(0)
        if median_value <= 0:
            # Not one bar in the window carried volume. That is a feed that does not report
            # volume, or a synthetic tape, far more often than a large cap that has not
            # printed for an hour - and a desk cannot tell the two apart from here. Refusing
            # on it would veto every tick of a volume-less feed; judging on it would invent
            # a number. So the desk abstains and says what it could not see.
            return self._evidence(
                request, Decimal(0), Decimal(0), "liquidity_unobserved:no_volume_in_window"
            )
        intended = _metric(request, "intended_position_notional") or Decimal(0)
        dead = _metric(request, "dead_tape_bars") or Decimal(0)
        participation = intended / median_value

        breaches: list[str] = []
        if spread_bps > self.policy.max_spread_bps:
            breaches.append(f"spread_bps={_quantized(spread_bps)}>{self.policy.max_spread_bps}")
        if dead >= self.policy.dead_tape_bars:
            breaches.append(f"dead_tape_bars={dead}>={self.policy.dead_tape_bars}")
        if participation > self.policy.max_participation:
            breaches.append(
                f"participation={_quantized(participation)}>{self.policy.max_participation}"
            )
        if breaches:
            return self._gate(request, Stance.AVOID, Decimal("0.90"), "liquidity_veto:" + ";".join(breaches))
        return self._gate(
            request,
            Stance.NEUTRAL,
            Decimal("0.70"),
            f"liquidity_ok:spread_bps={_quantized(spread_bps)};"
            f"participation={_quantized(participation)};dead_tape_bars={dead}",
        )


class RiskDeskAgent(SwarmAgent):
    """Refuses new risk the operator's own plan would not allow, before it is proposed.

    The warden enforces these limits after the decision; the desk states them before it,
    so the journal explains a hold in the plan's own terms and Atlas does not propose what
    the firewall is about to refuse. Every number is the plan's or the book's.
    """

    agent_id = "risk-desk"
    domain = AgentDomain.RISK

    _REQUIRED = (
        "book_daily_pnl_fraction",
        "book_drawdown_fraction",
        "book_gross_exposure_fraction",
        "book_symbol_exposure_fraction",
        "plan_max_daily_loss_fraction",
        "plan_max_drawdown_fraction",
        "plan_max_gross_exposure_fraction",
        "plan_max_position_fraction",
        "plan_trading_allowed",
    )

    def analyze(self, request: AgentAnalysisRequest) -> AgentEvidence:
        values = {key: _metric(request, key) for key in self._REQUIRED}
        if any(value is None for value in values.values()):
            missing = ",".join(key for key, value in values.items() if value is None)
            return self._evidence(request, Decimal(0), Decimal(0), f"risk_unobserved:{missing}")
        book: dict[str, Decimal] = {k: v for k, v in values.items() if v is not None}

        breaches: list[str] = []
        if book["plan_trading_allowed"] <= 0:
            breaches.append("plan_trading_disallowed")
        daily = book["book_daily_pnl_fraction"]
        loss_limit = book["plan_max_daily_loss_fraction"]
        loss_used = Decimal(0)
        if daily < 0:
            loss_used = (-daily / loss_limit) if loss_limit > 0 else Decimal(1)
            if loss_used >= 1:
                breaches.append(f"daily_loss_limit_reached:used={_quantized(loss_used)}")
        drawdown = book["book_drawdown_fraction"]
        drawdown_limit = book["plan_max_drawdown_fraction"]
        if drawdown > 0 and (drawdown_limit <= 0 or drawdown >= drawdown_limit):
            breaches.append(f"drawdown_limit_reached:{_quantized(drawdown)}>={drawdown_limit}")
        gross = book["book_gross_exposure_fraction"]
        position = book["plan_max_position_fraction"]
        gross_cap = book["plan_max_gross_exposure_fraction"]
        projected = gross + position
        if projected > gross_cap:
            breaches.append(f"gross_exposure_cap:projected={_quantized(projected)}>{gross_cap}")
        held = book["book_symbol_exposure_fraction"]
        if held >= position:
            breaches.append(f"position_full:held={_quantized(held)}>={position}")
        if breaches:
            return self._gate(request, Stance.AVOID, Decimal("0.90"), "risk_veto:" + ";".join(breaches))
        return self._gate(
            request,
            Stance.NEUTRAL,
            Decimal("0.70"),
            f"risk_ok:daily_loss_used={_quantized(loss_used)};drawdown={_quantized(drawdown)};"
            f"gross={_quantized(gross)};projected={_quantized(projected)};held={_quantized(held)}",
        )


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
