from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from quant_ai.agents.atlas import AtlasInvestmentAgent
from quant_ai.agents.swarm import AtlasCIOAgent
from quant_ai.agents.swarm_runtime import SwarmPaperTradingService
from quant_ai.domain.models import AssetClass, Instrument, Market, PortfolioSnapshot, RiskMode
from quant_ai.execution.audit import XAITraceLogger
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.execution.scheduler import AutonomousCadenceScheduler
from quant_ai.intelligence.pipeline import SwarmMarketAnalysisPipeline
from quant_ai.intelligence.sandbox import (
    SandboxFundamentalDataProvider,
    SandboxMacroIndicatorProvider,
    SandboxNewsSentimentProvider,
)
from quant_ai.llm.anthropic_client import AnthropicSwarmClient
from quant_ai.llm.challenger import ChallengerConsensusClient
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
        "rationale": ["fresh_tick_supports_upside", "specialists_are_net_positive"],
        "xai_proof": {
            "summary": "Paper-only buy consensus from fresh market context.",
            "supporting_factors": ["positive_momentum", "fresh_tick"],
            "risk_factors": ["model_uncertainty"],
        },
    }


def _mock_sdk(payload: dict[str, object]) -> SimpleNamespace:
    response = SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", name="trading_consensus", input=payload)]
    )
    return SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=response)))


def test_anthropic_client_uses_structured_tool_response() -> None:
    sdk = _mock_sdk(_payload())
    client = AnthropicSwarmClient(client=sdk, model="claude-sonnet-4-6")

    result = asyncio.run(client.generate_trading_consensus("AAPL market context"))
    signal, proof = client.parse_consensus(result)

    assert signal.stance.value == "BUY"
    assert signal.confidence == Decimal("0.82")
    assert proof.summary.startswith("Paper-only")
    sdk.messages.create.assert_awaited_once()
    kwargs = sdk.messages.create.await_args.kwargs
    assert kwargs["tool_choice"] == {"type": "tool", "name": "trading_consensus"}
    assert kwargs["model"] == "claude-sonnet-4-6"


@pytest.mark.parametrize("astra_shadow", [False, True])
def test_ten_minute_cadence_mocked_llm_executes_paper_trade(tmp_path, astra_shadow) -> None:
    now = datetime(2026, 9, 14, 15, 0, tzinfo=timezone.utc)
    sdk = _mock_sdk(_payload())
    llm = AnthropicSwarmClient(client=sdk, model="claude-sonnet-4-6")
    if astra_shadow:
        from test_astra_challenger import client, response
        # The challenger disagrees; only the existing primary proposal may execute.
        challenger, requests = client(response({**_payload(), "stance": "SELL"}))
        llm = ChallengerConsensusClient(llm, challenger)
    broker = PaperBrokerService(
        tmp_path / "anthropic-paper.db",
        starting_capital=Decimal(100000),
        slippage_bps=Decimal(0),
    )
    buffer = TickBuffer()
    buffer.put(
        LiveTick(
            "AAPL",
            Decimal("101.25"),
            Decimal(250000),
            Decimal("101.20"),
            Decimal("101.30"),
            now,
            "mock-websocket",
        )
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
        tick_reader=CadenceMarketReader(buffer),
    )
    scheduler = AutonomousCadenceScheduler(pipeline)
    plan = CapitalGoalEngine().recommend(
        CapitalPlanRequest(
            Decimal(100000),
            Decimal("0.82"),
            Decimal("0.20"),
            expected_edge=Decimal("0.02"),
            requested_mode=RiskMode.BALANCED,
        )
    )
    portfolio = PortfolioSnapshot(
        Decimal(100000), Decimal(0), Decimal(0), peak_equity=Decimal(100000)
    )
    instrument = Instrument("AAPL", Market.USA, AssetClass.EQUITY, "USD", "NASDAQ")

    brief = asyncio.run(
        scheduler.run_tick_async(
            instrument,
            now,
            plan,
            portfolio,
            quantity=10,
            country="USA",
            tenant_id="anthropic-test",
        )
    )

    assert scheduler.cadence.total_seconds() == 600
    assert brief.mode == "PAPER_TRADE"
    assert brief.paper_order_ids[0].startswith("PAPER-")
    assert len(broker.ledger_entries("anthropic-test")) == 1
    trace = pipeline.runtime.xai_logger.traces()[-1]
    assert trace.proposal["reference_price"] == "101.25"
    assert any(item.startswith("anthropic_model=") for item in trace.declared_rationales)
    sdk.messages.create.assert_awaited_once()
    if astra_shadow:
        assert len(requests) == 1
        comparison = trace.provenance["inference"]["model_comparison"]
        assert comparison["challenger"]["consensus"]["stance"] == "SELL"
        assert comparison["primary"]["consensus"]["stance"] == "BUY"
        logger = XAITraceLogger(tmp_path / "proofs")
        logger.record(trace)
        saved = json.loads((tmp_path / "proofs" / (trace.decision_id + ".json")).read_text())
        assert saved["provenance"]["inference"]["model_comparison"] == comparison
        assert saved["order_id"] == brief.paper_order_ids[0]
