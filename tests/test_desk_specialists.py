"""The liquidity desk and the risk desk: gates, not voters.

A week of NEUTRAL decisions came from specialists that could not see their inputs being
averaged in as zeros. The desks added here must not be able to do the same thing in the
other direction: a desk that only ever says "the quote is fine" must not count toward the
coverage floor, and must not dilute the specialists who hold a view. What a desk can do is
refuse - and when it does, the reason has to be in the plan's own numbers.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant_ai.agents.atlas import AtlasInvestmentAgent
from quant_ai.agents.contracts import AgentDomain, AgentEvidence, Stance
from quant_ai.agents.swarm import (
    AgentAnalysisRequest,
    LiquidityDeskAgent,
    LiquidityDeskPolicy,
    RiskDeskAgent,
    TechnicalQuantAgent,
)
from quant_ai.domain.models import AssetClass, Instrument, Market, PortfolioSnapshot
from quant_ai.intelligence.pipeline import SwarmMarketAnalysisPipeline
from quant_ai.intelligence.sandbox import (
    SandboxFundamentalDataProvider,
    SandboxMacroIndicatorProvider,
    SandboxNewsSentimentProvider,
)
from quant_ai.marketdata.feed import IndiaSandboxMarketDataFeed
from quant_ai.marketdata.models import Candle
from quant_ai.marketdata.ticker_stream import LiveTick
from quant_ai.planning.capital import CapitalGoalEngine, CapitalPlanRequest, RiskMode

NOW = datetime(2026, 9, 21, 4, 30, tzinfo=timezone.utc)
D = Decimal


def request(**metrics: Decimal | str) -> AgentAnalysisRequest:
    return AgentAnalysisRequest("INFY", Market.INDIA, AssetClass.EQUITY, NOW, dict(metrics), 30)


def liquid(**overrides: Decimal) -> dict[str, Decimal]:
    base = {
        "live_bid_ask_spread_bps": D("3.2"),
        "median_minute_traded_value": D("2500000"),
        "intended_position_notional": D("5000"),
        "dead_tape_bars": D(0),
    }
    base.update(overrides)
    return base


def book(**overrides: Decimal) -> dict[str, Decimal]:
    base = {
        "book_daily_pnl_fraction": D("0.001"),
        "book_drawdown_fraction": D(0),
        "book_gross_exposure_fraction": D("0.10"),
        "book_symbol_exposure_fraction": D(0),
        "plan_max_daily_loss_fraction": D("0.02"),
        "plan_max_drawdown_fraction": D("0.06"),
        "plan_max_gross_exposure_fraction": D("0.90"),
        "plan_max_position_fraction": D("0.05"),
        "plan_trading_allowed": D(1),
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------------------
# Liquidity desk
# --------------------------------------------------------------------------------------

def test_liquidity_desk_abstains_without_a_live_two_sided_quote() -> None:
    evidence = LiquidityDeskAgent().analyze(request(median_minute_traded_value=D(1)))
    assert evidence.domain is AgentDomain.LIQUIDITY
    assert evidence.stance is Stance.NEUTRAL and evidence.confidence == 0
    assert evidence.rationale[0] == "liquidity_unobserved:no_live_quote"


def test_liquidity_desk_passes_a_tight_active_quote() -> None:
    evidence = LiquidityDeskAgent().analyze(request(**liquid()))
    assert evidence.stance is Stance.NEUTRAL
    assert evidence.confidence == D("0.70")
    assert evidence.rationale[0].startswith("liquidity_ok:spread_bps=3.2000;participation=0.0020")
    assert evidence.expected_return == 0 and evidence.expected_risk == 0


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"live_bid_ask_spread_bps": D("31")}, "spread_bps=31.0000>30"),
        ({"dead_tape_bars": D(5)}, "dead_tape_bars=5>=5"),
        ({"intended_position_notional": D("700000")}, "participation=0.2800>0.25"),
    ],
)
def test_liquidity_desk_vetoes_and_names_the_breach(override: dict[str, Decimal], fragment: str) -> None:
    evidence = LiquidityDeskAgent().analyze(request(**liquid(**override)))
    assert evidence.stance is Stance.AVOID
    assert evidence.confidence == D("0.90")
    assert evidence.rationale[0].startswith("liquidity_veto:")
    assert fragment in evidence.rationale[0]


def test_liquidity_desk_abstains_rather_than_vetoes_when_no_bar_carries_volume() -> None:
    """A window with no volume at all is a feed that does not report it, not a dead stock.
    Vetoing on it would hold every tick of such a feed; the desk abstains and names it."""
    evidence = LiquidityDeskAgent().analyze(
        request(**liquid(median_minute_traded_value=D(0), dead_tape_bars=D(30)))
    )
    assert evidence.stance is Stance.NEUTRAL and evidence.confidence == 0
    assert evidence.rationale[0] == "liquidity_unobserved:no_volume_in_window"


def test_liquidity_desk_collapses_to_silence_on_stale_price_data() -> None:
    # Stale inputs silence a gate exactly as they silence a scoring specialist: a veto on
    # a stale quote would be as invented as a pass on one.
    evidence = LiquidityDeskAgent().analyze(
        request(**liquid(live_bid_ask_spread_bps=D("90")), freshness_multiplier=D("0.10"))
    )
    assert evidence.stance is Stance.NEUTRAL and evidence.confidence == 0
    assert "capital_preservation_stale_or_missing_data" in evidence.rationale


def test_liquidity_desk_policy_refuses_meaningless_thresholds() -> None:
    with pytest.raises(ValueError, match="liquidity_desk_policy_invalid"):
        LiquidityDeskPolicy(max_spread_bps=D(0))
    with pytest.raises(ValueError, match="liquidity_desk_policy_invalid"):
        LiquidityDeskPolicy(dead_tape_bars=0)


# --------------------------------------------------------------------------------------
# Risk desk
# --------------------------------------------------------------------------------------

def test_risk_desk_abstains_and_names_the_missing_book_input() -> None:
    incomplete = book()
    del incomplete["plan_trading_allowed"]
    evidence = RiskDeskAgent().analyze(request(**incomplete))
    assert evidence.domain is AgentDomain.RISK
    assert evidence.stance is Stance.NEUTRAL and evidence.confidence == 0
    assert evidence.rationale[0] == "risk_unobserved:plan_trading_allowed"


def test_risk_desk_passes_a_book_with_room() -> None:
    evidence = RiskDeskAgent().analyze(request(**book()))
    assert evidence.stance is Stance.NEUTRAL
    assert evidence.confidence == D("0.70")
    assert evidence.rationale[0] == (
        "risk_ok:daily_loss_used=0.0000;drawdown=0.0000;gross=0.1000;projected=0.1500;held=0.0000"
    )


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"plan_trading_allowed": D(0)}, "plan_trading_disallowed"),
        ({"book_daily_pnl_fraction": D("-0.02")}, "daily_loss_limit_reached:used=1.0000"),
        ({"book_daily_pnl_fraction": D("-0.001"), "plan_max_daily_loss_fraction": D(0)},
         "daily_loss_limit_reached:used=1.0000"),
        ({"book_drawdown_fraction": D("0.06")}, "drawdown_limit_reached:0.0600>=0.06"),
        ({"book_gross_exposure_fraction": D("0.86")}, "gross_exposure_cap:projected=0.9100>0.90"),
        ({"book_symbol_exposure_fraction": D("0.05")}, "position_full:held=0.0500>=0.05"),
    ],
)
def test_risk_desk_vetoes_in_the_plans_own_terms(override: dict[str, Decimal], fragment: str) -> None:
    evidence = RiskDeskAgent().analyze(request(**book(**override)))
    assert evidence.stance is Stance.AVOID
    assert evidence.confidence == D("0.90")
    assert evidence.rationale[0].startswith("risk_veto:")
    assert fragment in evidence.rationale[0]


def test_risk_desk_does_not_veto_a_loss_inside_the_budget() -> None:
    evidence = RiskDeskAgent().analyze(request(**book(book_daily_pnl_fraction=D("-0.01"))))
    assert evidence.stance is Stance.NEUTRAL
    assert "daily_loss_used=0.5000" in evidence.rationale[0]


# --------------------------------------------------------------------------------------
# Atlas: gates are outside the floor and the mean, inside the veto
# --------------------------------------------------------------------------------------

def vote(agent: str, domain: AgentDomain, stance: Stance, confidence: str = "0.75") -> AgentEvidence:
    return AgentEvidence(
        agent, domain, "INFY", stance, D(confidence), D("0.04"), D("0.02"), ("evidence",), NOW, 60,
    )


def gate(agent: str, domain: AgentDomain, stance: Stance, rationale: str) -> AgentEvidence:
    return AgentEvidence(
        agent, domain, "INFY", stance, D("0.70" if stance is Stance.NEUTRAL else "0.90"),
        D(0), D(0), (rationale,), NOW, 60,
    )


THREE_BUYS = (
    vote("geopolitical-analyst", AgentDomain.NEWS, Stance.BUY),
    vote("technical-quant-mas", AgentDomain.TECHNICAL, Stance.BUY),
    vote("indian-equities", AgentDomain.COUNTRY, Stance.BUY),
)
OPEN_GATES = (
    gate("liquidity-desk", AgentDomain.LIQUIDITY, Stance.NEUTRAL, "liquidity_ok:spread_bps=2"),
    gate("risk-desk", AgentDomain.RISK, Stance.NEUTRAL, "risk_ok:gross=0.1"),
)


def test_open_gates_neither_dilute_the_consensus_nor_count_as_coverage() -> None:
    atlas = AtlasInvestmentAgent()
    without = atlas.decide("INFY", THREE_BUYS, NOW)
    with_gates = atlas.decide("INFY", THREE_BUYS + OPEN_GATES, NOW)
    assert without.action is Stance.BUY and with_gates.action is Stance.BUY
    assert with_gates.confidence == without.confidence
    assert with_gates.expected_return == without.expected_return
    assert [line for line in with_gates.rationale if line.startswith("weighted_consensus=")] == [
        line for line in without.rationale if line.startswith("weighted_consensus=")
    ]
    assert "gate_specialists=liquidity-desk:liquidity_ok:spread_bps=2;risk-desk:risk_ok:gross=0.1" in with_gates.rationale
    assert "abstained_specialists=none" in with_gates.rationale

    # Two voters plus two open gates is still two voters.
    short = atlas.decide("INFY", THREE_BUYS[:2] + OPEN_GATES, NOW)
    assert short.action is Stance.NEUTRAL
    assert "insufficient_agent_coverage" in short.rationale


def test_a_desk_veto_holds_the_cycle_whatever_the_voters_say() -> None:
    closed = gate("liquidity-desk", AgentDomain.LIQUIDITY, Stance.AVOID, "liquidity_veto:spread_bps=40>30")
    decision = AtlasInvestmentAgent().decide("INFY", THREE_BUYS + (closed,), NOW)
    assert decision.action is Stance.NEUTRAL
    assert "specialist_veto" in decision.rationale
    assert "liquidity-desk" in decision.dissenting_agents


# --------------------------------------------------------------------------------------
# Pipeline: the desks are on the roster and their inputs are derived, not guessed
# --------------------------------------------------------------------------------------

INFY = Instrument("INFY", Market.INDIA, AssetClass.EQUITY, "INR", "NSE")


def plan(trading_allowed: bool = True):
    built = CapitalGoalEngine().recommend(
        CapitalPlanRequest(
            D(100000), D("0.8"), D("0.2"), expected_edge=D("0.02"), requested_mode=RiskMode.BALANCED,
        )
    )
    if built.trading_allowed == trading_allowed:
        return built
    from dataclasses import replace

    return replace(built, trading_allowed=trading_allowed)


def candles(volumes: tuple[int, ...], close: str = "1500") -> tuple[Candle, ...]:
    start = NOW - timedelta(minutes=len(volumes))
    return tuple(
        Candle(INFY, start + timedelta(minutes=i), D(close), D(close), D(close), D(close), D(v))
        for i, v in enumerate(volumes)
    )


def pipeline() -> SwarmMarketAnalysisPipeline:
    return SwarmMarketAnalysisPipeline(
        IndiaSandboxMarketDataFeed(),
        SandboxNewsSentimentProvider(),
        SandboxFundamentalDataProvider(),
        SandboxMacroIndicatorProvider(),
    )


def test_roster_carries_both_desks_and_keeps_the_technical_agent_as_root() -> None:
    ids = [agent.agent_id for agent in pipeline().agents]
    assert "liquidity-desk" in ids and "risk-desk" in ids
    assert ids[-1] == TechnicalQuantAgent.agent_id
    assert len(ids) == 7


def test_desk_metrics_are_derived_from_bars_quote_plan_and_book() -> None:
    capital_plan = plan()
    portfolio = PortfolioSnapshot(
        D(100000), D(0), D(12000), peak_equity=D(104000),
        symbol_exposure={"INFY": D(4000)}, daily_total_pnl=D(-500),
    )
    tick = LiveTick("INFY", D("1500"), D(10), D("1499.5"), D("1500.5"), NOW, "kite")
    bars = candles((100, 300, 200, 0, 0))
    metrics = SwarmMarketAnalysisPipeline._desk_metrics("INFY", bars, capital_plan, portfolio, tick)

    assert metrics["median_minute_traded_value"] == D(1500) * 100  # median of 0,0,100,200,300 -> 100
    assert metrics["dead_tape_bars"] == D(2)
    assert metrics["intended_position_notional"] == capital_plan.max_position_amount
    assert metrics["live_bid_ask_spread_bps"] == D(1) / D(1500) * D(10000)
    assert metrics["book_gross_exposure_fraction"] == D("0.12")
    assert metrics["book_symbol_exposure_fraction"] == D("0.04")
    assert metrics["book_daily_pnl_fraction"] == D("-0.005")
    assert metrics["book_drawdown_fraction"] == D(4000) / D(104000)
    assert metrics["plan_max_daily_loss_fraction"] == capital_plan.max_daily_loss_fraction
    assert metrics["plan_trading_allowed"] == D(1)


def test_desk_metrics_leave_out_what_was_not_observed() -> None:
    capital_plan = plan()
    empty_book = PortfolioSnapshot(D(0), D(0), D(0))
    metrics = SwarmMarketAnalysisPipeline._desk_metrics("INFY", (), capital_plan, empty_book, None)
    assert "median_minute_traded_value" not in metrics and "dead_tape_bars" not in metrics
    assert "live_bid_ask_spread_bps" not in metrics
    assert not any(key.startswith(("book_", "plan_")) for key in metrics)
    assert metrics == {"intended_position_notional": capital_plan.max_position_amount}

    one_sided = LiveTick("INFY", D("1500"), D(10), D("1499.5"), None, NOW, "kite")
    with_tick = SwarmMarketAnalysisPipeline._desk_metrics("INFY", (), capital_plan, empty_book, one_sided)
    assert "live_bid_ask_spread_bps" not in with_tick


def test_a_full_cycle_records_both_desks_beside_the_five_specialists() -> None:
    result = pipeline().run(INFY, NOW, plan(), PortfolioSnapshot(D(100000), D(0), D(0)), country="India")
    by_agent = {item.agent_id: item for item in result.evidence}
    assert {"liquidity-desk", "risk-desk"} <= set(by_agent)
    # No live quote on the synchronous path: the liquidity desk says so and abstains.
    assert by_agent["liquidity-desk"].rationale[0] == "liquidity_unobserved:no_live_quote"
    # The book is empty and the plan allows trading: the risk desk passes, with numbers.
    assert by_agent["risk-desk"].stance is Stance.NEUTRAL
    assert by_agent["risk-desk"].rationale[0].startswith("risk_ok:daily_loss_used=0.0000")


# --------------------------------------------------------------------------------------
# Runtime manifest: the desks are vouched for, and their thresholds are part of the strategy
# --------------------------------------------------------------------------------------

def test_manifest_vouches_for_both_desks_and_fingerprints_liquidity_thresholds() -> None:
    """The manifest refuses every entry while the roster holds a specialist it cannot
    describe. Both desks must be described, and a changed liquidity threshold must change
    the strategy hash, because it changes what the book is allowed to trade into."""
    from quant_ai.governance import runtime_manifest
    from quant_ai.governance.runtime_manifest import stable

    source = __import__("inspect").getsource(runtime_manifest)
    assert '"LiquidityDeskAgent"' in source and '"RiskDeskAgent"' in source
    tight = stable(LiquidityDeskAgent().policy)
    loose = stable(LiquidityDeskAgent(LiquidityDeskPolicy(max_spread_bps=D(60))).policy)
    assert tight != loose
    assert stable(RiskDeskAgent().domain) == stable(AgentDomain.RISK)
