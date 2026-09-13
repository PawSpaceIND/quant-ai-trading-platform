from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

from quant_ai.agents.atlas import AtlasInvestmentAgent
from quant_ai.agents.swarm import AtlasCIOAgent
from quant_ai.agents.swarm_runtime import SwarmPaperTradingService
from quant_ai.domain.models import (
    AssetClass,
    Instrument,
    Market,
    OrderIntent,
    PortfolioSnapshot,
    RiskMode,
    Side,
)
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.execution.scheduler import AutonomousCadenceScheduler
from quant_ai.intelligence.pipeline import SwarmMarketAnalysisPipeline
from quant_ai.intelligence.sandbox import (
    SandboxFundamentalDataProvider,
    SandboxMacroIndicatorProvider,
    SandboxNewsSentimentProvider,
)
from quant_ai.llm.anthropic_client import AnthropicSwarmClient
from quant_ai.marketdata.feed import UsaSandboxMarketDataFeed
from quant_ai.marketdata.ticker_stream import LiveTick, TickBuffer
from quant_ai.orchestration.cadence import CadenceMarketReader
from quant_ai.planning.capital import CapitalGoalEngine, CapitalPlanRequest


def _payload() -> dict[str, object]:
    return {
        "stance": "BUY",
        "confidence": 0.82,
        "expected_return": 0.03,
        "expected_risk": 0.02,
        "rationale": ["fresh_tick_supports_upside"],
        "xai_proof": {
            "summary": "Paper-only consensus.",
            "supporting_factors": ["fresh_tick"],
            "risk_factors": ["model_uncertainty"],
        },
    }


def _sdk(payload: object | None = None, *, side_effect: object | None = None) -> SimpleNamespace:
    response = SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", name="trading_consensus", input=payload)]
    )
    create = AsyncMock(side_effect=side_effect) if side_effect is not None else AsyncMock(return_value=response)
    return SimpleNamespace(messages=SimpleNamespace(create=create))


def _plan():
    return CapitalGoalEngine().recommend(
        CapitalPlanRequest(
            Decimal(100000),
            Decimal("0.82"),
            Decimal("0.20"),
            expected_edge=Decimal("0.02"),
            requested_mode=RiskMode.BALANCED,
        )
    )


def _portfolio() -> PortfolioSnapshot:
    return PortfolioSnapshot(Decimal(100000), Decimal(0), Decimal(0), peak_equity=Decimal(100000))


def _instrument() -> Instrument:
    return Instrument("AAPL", Market.USA, AssetClass.EQUITY, "USD", "NASDAQ")


def _pipeline(tmp_path, llm: AnthropicSwarmClient, buffer: TickBuffer):
    broker = PaperBrokerService(
        tmp_path / "chaos-paper.db",
        starting_capital=Decimal(100000),
        slippage_bps=Decimal(0),
    )
    runtime = SwarmPaperTradingService(
        cio=AtlasCIOAgent(AtlasInvestmentAgent(llm_client=llm)),
        broker=broker,
    )
    pipeline = SwarmMarketAnalysisPipeline(
        UsaSandboxMarketDataFeed(),
        SandboxNewsSentimentProvider(),
        SandboxFundamentalDataProvider(),
        SandboxMacroIndicatorProvider(),
        runtime=runtime,
        tick_reader=CadenceMarketReader(buffer, max_tick_age=timedelta(minutes=2)),
    )
    return broker, pipeline


def _fresh_tick(now: datetime) -> LiveTick:
    return LiveTick(
        "AAPL",
        Decimal("101.25"),
        Decimal(250000),
        Decimal("101.20"),
        Decimal("101.30"),
        now,
        "chaos-websocket",
    )


def test_severed_stream_vetoes_before_llm_and_consumes_cadence(tmp_path) -> None:
    now = datetime(2026, 9, 14, 15, 0, tzinfo=timezone.utc)
    sdk = _sdk(_payload())
    llm = AnthropicSwarmClient(client=sdk)
    buffer = TickBuffer()
    buffer.put(
        LiveTick(
            "AAPL",
            Decimal("101.25"),
            Decimal(250000),
            Decimal("101.20"),
            Decimal("101.30"),
            now - timedelta(minutes=3),
            "severed-websocket",
        )
    )
    broker, pipeline = _pipeline(tmp_path, llm, buffer)
    scheduler = AutonomousCadenceScheduler(pipeline, cadence=timedelta(minutes=10))

    brief = asyncio.run(
        scheduler.run_tick_async(
            _instrument(), now, _plan(), _portfolio(), quantity=10, country="USA", tenant_id="stale"
        )
    )

    sdk.messages.create.assert_not_awaited()
    assert brief.risk_decision == "Stale Market Data"
    assert brief.paper_order_ids == ()
    assert broker.ledger_entries("stale") == ()
    assert not scheduler.is_due(now + timedelta(minutes=9, seconds=59))
    assert scheduler.is_due(now + timedelta(minutes=10))
    assert pipeline.runtime.xai_logger.traces()[-1].risk_verdict["reason"] == "Stale Market Data"


def test_anthropic_timeout_and_529_return_skipped_consensus_proof() -> None:
    class OverloadedError(Exception):
        status_code = 529

    async def hangs_forever(**kwargs: object) -> object:
        _ = kwargs
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    timeout_client = AnthropicSwarmClient(
        client=SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(side_effect=hangs_forever))),
        timeout_seconds=0.01,
    )
    timeout_payload = asyncio.run(timeout_client.generate_trading_consensus("AAPL"))
    _, timeout_proof = timeout_client.parse_consensus(timeout_payload)
    assert timeout_proof.summary == "Consensus Skipped: API Timeout"

    overloaded_client = AnthropicSwarmClient(client=_sdk(side_effect=OverloadedError("overloaded")))
    overloaded_payload = asyncio.run(overloaded_client.generate_trading_consensus("AAPL"))
    _, overloaded_proof = overloaded_client.parse_consensus(overloaded_payload)
    assert overloaded_proof.summary == "Consensus Skipped: API Timeout"


def test_timeout_exits_cycle_with_xai_proof_and_no_paper_order(tmp_path) -> None:
    now = datetime(2026, 9, 14, 15, 0, tzinfo=timezone.utc)
    sdk = _sdk(side_effect=TimeoutError("sdk timeout"))
    llm = AnthropicSwarmClient(client=sdk)
    buffer = TickBuffer()
    buffer.put(_fresh_tick(now))
    broker, pipeline = _pipeline(tmp_path, llm, buffer)
    scheduler = AutonomousCadenceScheduler(pipeline)

    brief = asyncio.run(
        scheduler.run_tick_async(
            _instrument(), now, _plan(), _portfolio(), quantity=10, country="USA", tenant_id="timeout"
        )
    )

    assert brief.paper_order_ids == ()
    assert broker.ledger_entries("timeout") == ()
    trace = pipeline.runtime.xai_logger.traces()[-1]
    assert "Consensus Skipped: API Timeout" in trace.declared_rationales
    assert "xai_summary=Consensus Skipped: API Timeout" in trace.declared_rationales


def test_hallucinated_schema_is_rejected_before_paper_broker(tmp_path) -> None:
    now = datetime(2026, 9, 14, 15, 0, tzinfo=timezone.utc)
    malformed = _payload()
    malformed["expected_return"] = "0.03"
    sdk = _sdk(malformed)
    llm = AnthropicSwarmClient(client=sdk)
    buffer = TickBuffer()
    buffer.put(_fresh_tick(now))
    broker, pipeline = _pipeline(tmp_path, llm, buffer)
    scheduler = AutonomousCadenceScheduler(pipeline)

    brief = asyncio.run(
        scheduler.run_tick_async(
            _instrument(), now, _plan(), _portfolio(), quantity=10, country="USA", tenant_id="schema"
        )
    )

    sdk.messages.create.assert_awaited_once()
    assert brief.paper_order_ids == ()
    assert broker.ledger_entries("schema") == ()
    trace = pipeline.runtime.xai_logger.traces()[-1]
    assert "Consensus Skipped: Invalid Schema" in trace.declared_rationales


def test_sqlite_lock_retries_three_times_then_completes_paper_trade(tmp_path) -> None:
    database = tmp_path / "locked.db"
    sleeps: list[float] = []
    locker: sqlite3.Connection | None = None

    def release_after_third_retry(delay: float) -> None:
        nonlocal locker
        sleeps.append(delay)
        if len(sleeps) == 3 and locker is not None:
            locker.commit()
            locker.close()
            locker = None

    broker = PaperBrokerService(
        database,
        starting_capital=Decimal(100000),
        slippage_bps=Decimal(0),
        lock_retries=3,
        lock_backoff_seconds=0.001,
        sleep_fn=release_after_third_retry,
        jitter_fn=lambda low, high: (low + high) / 2,
    )
    broker._connection.execute("PRAGMA busy_timeout = 1")
    locker = sqlite3.connect(database, timeout=0)
    locker.execute("BEGIN EXCLUSIVE")
    order = OrderIntent(
        "AAPL", Market.USA, Side.BUY, 1, Decimal(100), "chaos", AssetClass.EQUITY, "locked"
    )

    fill = broker.submit(order)

    assert fill.status == "FILLED"
    assert len(sleeps) == 3
    assert sleeps[0] < sleeps[1] < sleeps[2]
    assert len(broker.ledger_entries("locked")) == 1
