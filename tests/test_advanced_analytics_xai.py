from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from quant_ai.agents.contracts import AgentDomain, AgentEvidence, Stance
from quant_ai.agents.swarm import AgentAnalysisRequest, TradeProposal
from quant_ai.agents.swarm_runtime import SwarmPaperTradingService
from quant_ai.analytics.attribution import AgentAttributionEngine
from quant_ai.analytics.metrics import (
    historical_var,
    maximum_drawdown,
    sharpe_ratio,
    sortino_ratio,
    win_loss_ratio,
)
from quant_ai.domain.models import AssetClass, Instrument, Market, PortfolioSnapshot, RiskMode, Side
from quant_ai.execution.audit import XAITraceLogger
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.intelligence.adversarial import AdversarialStressAgent
from quant_ai.intelligence.regime import MarketRegime, MarketRegimeDetector
from quant_ai.intelligence.resilience import TokenBucketRateLimiter
from quant_ai.marketdata.models import Candle
from quant_ai.planning.capital import CapitalGoalEngine, CapitalPlanRequest


def instrument() -> Instrument:
    return Instrument("AAPL", Market.USA, AssetClass.EQUITY, "USD", "NASDAQ")


def candles(prices: tuple[Decimal, ...], spread: Decimal = Decimal("0.5")) -> tuple[Candle, ...]:
    start = datetime(2026, 9, 14, 13, tzinfo=timezone.utc)
    return tuple(
        Candle(
            instrument(),
            start + timedelta(minutes=index),
            price,
            price + spread,
            max(Decimal("0.01"), price - spread),
            price,
            Decimal(1000),
        )
        for index, price in enumerate(prices)
    )


def plan():
    return CapitalGoalEngine().recommend(
        CapitalPlanRequest(
            Decimal(100000),
            Decimal("0.80"),
            Decimal("0.20"),
            expected_edge=Decimal("0.02"),
            requested_mode=RiskMode.BALANCED,
        )
    )


def test_performance_metrics_and_var_are_mathematically_bounded() -> None:
    returns = (Decimal("0.01"), Decimal("-0.02"), Decimal("0.03"), Decimal("-0.01"))
    # Four observations cannot support an annualised ratio, so none is reported.
    assert sharpe_ratio(returns) is None
    assert sortino_ratio(returns) is None
    long_enough = returns * 10
    assert sharpe_ratio(long_enough) is not None
    assert sortino_ratio(long_enough) is not None
    assert maximum_drawdown((Decimal(100), Decimal(110), Decimal(99), Decimal(120))) == Decimal("0.1")
    assert win_loss_ratio((Decimal(1), Decimal(2), Decimal(-1), Decimal(-3))) == Decimal(1)
    assert historical_var(returns, Decimal("0.95")) == Decimal("0.02")
    assert historical_var(returns, Decimal(0)) == 0


def test_regime_detector_classifies_trend_and_tightens_exposure() -> None:
    detector = MarketRegimeDetector()
    bull = detector.detect(candles(tuple(Decimal(100 + index) for index in range(40))))
    assert bull.regime == MarketRegime.BULL_TRENDING
    assert bull.gross_exposure_multiplier == Decimal("1.00")
    crisis_prices = tuple(Decimal(100 + (10 if index % 2 else -10)) for index in range(20))
    crisis = detector.detect(candles(crisis_prices, Decimal(5)))
    assert crisis.regime == MarketRegime.HIGH_VOL_CRISIS
    tightened = detector.apply_to_plan(plan(), crisis)
    assert tightened.max_gross_exposure_fraction < plan().max_gross_exposure_fraction


def test_agent_attribution_bounds_conviction_weight() -> None:
    engine = AgentAttributionEngine()
    engine.record(("agent-a",), Decimal(10))
    engine.record(("agent-a",), Decimal(-2))
    rows = engine.attribution()
    assert rows[0].hit_rate == Decimal("0.5")
    assert Decimal("0.75") <= rows[0].conviction_weight <= Decimal("1.25")
    evidence = AgentEvidence(
        "agent-a", AgentDomain.TECHNICAL, "AAPL", Stance.BUY, Decimal("0.8"),
        Decimal("0.02"), Decimal("0.01"), ("test",),
        datetime(2026, 9, 14, 14, tzinfo=timezone.utc), 0,
    )
    weighted = engine.weight_evidence((evidence,))[0]
    assert Decimal(0) <= weighted.confidence <= Decimal(1)
    assert any(item.startswith("attribution_weight=") for item in weighted.rationale)


class VulnerableCIO:
    def propose(self, request, evidence, **kwargs):
        return TradeProposal(
            "stress-decision", request.subject, request.market, "USA", request.asset_class,
            Side.BUY, 100, Decimal(100), Decimal(95), Decimal(110), Decimal("0.9"),
            Decimal("0.02"), Decimal("0.01"), ("high_conviction_test",),
        )


def test_stress_agent_intercepts_before_warden_and_blocks_trade(tmp_path) -> None:
    broker = PaperBrokerService(tmp_path / "stress.db", starting_capital=Decimal(100000), slippage_bps=Decimal(0))
    service = SwarmPaperTradingService(
        cio=VulnerableCIO(), broker=broker,
        stress_agent=AdversarialStressAgent(Decimal("0.001")),
    )
    request = AgentAnalysisRequest(
        "AAPL", Market.USA, AssetClass.EQUITY,
        datetime(2026, 9, 14, 14, tzinfo=timezone.utc), {}, 0,
    )
    portfolio = PortfolioSnapshot(Decimal(100000), Decimal(0), Decimal(0), peak_equity=Decimal(100000))
    result = service.execute(
        request, (), plan(), portfolio, quantity=100, reference_price=Decimal(100),
        stop_price=Decimal(95), take_profit_price=Decimal(110), country="USA", tenant_id="stress",
    )
    assert not result.stress_verdict.passed
    assert result.stress_verdict.flags == ("STRESS_VETO",)
    assert result.risk_decision.reason == "STRESS_VETO"
    assert result.fill is None
    assert not broker.ledger_entries("stress")


def test_xai_logger_writes_complete_human_readable_reports(tmp_path) -> None:
    logger = XAITraceLogger(tmp_path)
    broker = PaperBrokerService(tmp_path / "xai.db", starting_capital=Decimal(100000), slippage_bps=Decimal(0))
    service = SwarmPaperTradingService(broker=broker, xai_logger=logger)
    now = datetime(2026, 9, 14, 14, tzinfo=timezone.utc)
    request = AgentAnalysisRequest("AAPL", Market.USA, AssetClass.EQUITY, now, {}, 0)
    evidence = tuple(
        AgentEvidence(
            f"agent-{index}", AgentDomain.TECHNICAL, "AAPL", Stance.BUY,
            Decimal("0.8"), Decimal("0.02"), Decimal("0.01"), ("declared_factor",), now, 0,
        )
        for index in range(4)
    )
    portfolio = PortfolioSnapshot(Decimal(100000), Decimal(0), Decimal(0), peak_equity=Decimal(100000))
    result = service.execute(
        request, evidence, plan(), portfolio, quantity=10, reference_price=Decimal(100),
        stop_price=Decimal(95), take_profit_price=Decimal(110), country="USA", tenant_id="xai",
    )
    trace = result.xai_trace
    payload = json.loads(logger.to_json(trace))
    assert {"input_matrix", "confidence_distribution", "proposal", "stress_verdict", "risk_verdict"} <= payload.keys()
    assert payload["decision_id"] == trace.decision_id
    assert (tmp_path / f"{trace.decision_id}.json").exists()
    report = (tmp_path / f"{trace.decision_id}.md").read_text()
    assert "Specialist evidence" in report
    assert "Declared Atlas rationales" in report


def test_provider_rate_limiter_hard_caps_at_ten_ops() -> None:
    limiter = TokenBucketRateLimiter(100, 100)
    assert limiter.rate == 10.0
    assert limiter.capacity == 1.0
