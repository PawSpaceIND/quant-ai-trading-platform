"""Synthetic cutoff regression. No live provider/model/order is exercised."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
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

T0 = datetime(2026, 9, 14, 15, tzinfo=timezone.utc)
D = Decimal


def tick(seconds=0, *, symbol="AAPL", price="101.25"):
    price = D(price)
    return LiveTick(symbol, price, D(250000), price - D(".05"), price + D(".05"),
                    T0 + timedelta(seconds=seconds), "synthetic-cutoff-test")


def test_newer_arrival_must_not_make_the_eligible_cutoff_tick_future():
    clock = [T0]
    buffer = TickBuffer(clock=lambda: clock[0])
    before, after = tick(-1), tick(3, price="102.25")
    assert buffer.put(before)
    clock[0] += timedelta(seconds=3)
    assert buffer.put(after)
    reader = CadenceMarketReader(buffer)
    assert reader.market_data_status("AAPL", T0) == (before, None)
    assert buffer.latest("AAPL") is after
    assert reader.market_data_status("AAPL", clock[0]) == (after, None)


def test_tick_arriving_while_headlines_await_keeps_original_cutoff(tmp_path):
    """Exercise unchanged pipeline + runtime with an actual asynchronous tick arrival."""
    async def scenario():
        clock = [T0]
        buffer = TickBuffer(clock=lambda: clock[0])
        original, arriving = tick(), tick(3, price="102.25")
        assert buffer.put(original)
        entered, delivered = asyncio.Event(), asyncio.Event()

        class YieldingScorer:
            async def score(self, subject, headlines):
                entered.set()
                await delivered.wait()
                return ()  # Existing fallback retains deterministic synthetic headline scores.

        payload = {
            "stance": "NEUTRAL", "confidence": .82,
            "expected_return": .03, "expected_risk": .02,
            "rationale": ["Synthetic nontrading consensus reached with cutoff-bound evidence"],
            "xai_proof": {"summary": "Synthetic timestamp regression, not market evidence",
                          "supporting_factors": ["cutoff-bound tick"], "risk_factors": ["synthetic"]},
        }
        response = SimpleNamespace(content=[SimpleNamespace(
            type="tool_use", name="trading_consensus", input=payload)])
        sdk = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=response)))
        llm = AnthropicSwarmClient(client=sdk, model="synthetic-model")
        broker = PaperBrokerService(tmp_path / "paper.db", starting_capital=D(100000),
                                    slippage_bps=D(0))
        logger = XAITraceLogger(tmp_path / "proofs")
        runtime = SwarmPaperTradingService(cio=AtlasCIOAgent(AtlasInvestmentAgent(llm_client=llm)),
                                          broker=broker, xai_logger=logger)
        reader = CadenceMarketReader(buffer)
        pipeline = SwarmMarketAnalysisPipeline(
            UsaSandboxMarketDataFeed(), SandboxNewsSentimentProvider(),
            SandboxFundamentalDataProvider(), SandboxMacroIndicatorProvider(),
            runtime=runtime, tick_reader=reader, headline_scorer=YieldingScorer(),
        )
        captured = []
        original_status = reader.market_data_status

        def capture(symbol, now=None):
            selected, issue = original_status(symbol, now)
            captured.append((now, clock[0], buffer.latest(symbol), selected, issue))
            return selected, issue

        reader.market_data_status = capture
        plan = CapitalGoalEngine().recommend(CapitalPlanRequest(
            D(100000), D(".82"), D(".20"), expected_edge=D(".02"),
            requested_mode=RiskMode.BALANCED))
        portfolio = PortfolioSnapshot(D(100000), D(0), D(0), peak_equity=D(100000))
        instrument = Instrument("AAPL", Market.USA, AssetClass.EQUITY, "USD", "NASDAQ")

        async def stream_arrival():
            await entered.wait()
            clock[0] = T0 + timedelta(seconds=3)
            assert buffer.put(arriving)
            delivered.set()

        try:
            result, _ = await asyncio.wait_for(asyncio.gather(
                pipeline.run_async(instrument, T0, plan, portfolio, quantity=10,
                                   country="USA", tenant_id="synthetic-cutoff"),
                stream_arrival(),
            ), timeout=10)
            assert captured == [(T0, T0 + timedelta(seconds=3), arriving, original, None)]
            sdk.messages.create.assert_awaited_once()
            assert result.features["live_ltp"] == original.ltp
            assert result.execution.proposal.reference_price == original.ltp
            assert result.execution.fill is None
            assert broker.ledger_entries("synthetic-cutoff") == ()
        finally:
            broker.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("seconds,reason", [(-120, None), (-121, "Stale Market Data")])
def test_asof_selection_keeps_original_two_minute_age_limit(seconds, reason):
    buffer = TickBuffer(clock=lambda: T0 + timedelta(seconds=10))
    prior = tick(seconds)
    assert buffer.put(prior) and buffer.put(tick(10))
    expected = (prior, None) if reason is None else (None, reason)
    assert CadenceMarketReader(buffer).market_data_status("AAPL", T0) == expected


def test_no_pre_cutoff_tick_remains_refused():
    buffer = TickBuffer(clock=lambda: T0 + timedelta(seconds=10))
    assert buffer.put(tick(10))
    assert CadenceMarketReader(buffer).market_data_status("AAPL", T0) == (None, "Future Market Data")


def test_real_future_arrival_is_still_rejected_by_ingestion():
    buffer = TickBuffer(clock=lambda: T0)
    assert buffer.put(tick())
    assert buffer.put(tick(1)) is False
    assert buffer.integrity()["rejected"] == {"future_tick": 1}
    assert CadenceMarketReader(buffer).market_data_status("AAPL", T0) == (tick(), None)


def test_generic_buffer_without_history_stays_fail_closed():
    reader = CadenceMarketReader(SimpleNamespace(latest=lambda _: tick(1)))
    assert reader.market_data_status("AAPL", T0) == (None, "Future Market Data")


def test_selector_does_not_fabricate_evicted_history():
    buffer = TickBuffer(maxlen=2, clock=lambda: T0 + timedelta(seconds=4))
    for stamp in (-1, 1, 2):
        assert buffer.put(tick(stamp))
    assert CadenceMarketReader(buffer).market_data_status("AAPL", T0) == (None, "Future Market Data")


def test_old_latest_for_other_symbol_survives_shared_history_eviction():
    buffer = TickBuffer(maxlen=2, clock=lambda: T0 + timedelta(seconds=4))
    prior = tick(-1, symbol="INFY")
    assert buffer.put(prior)
    for stamp in (1, 2, 3):
        assert buffer.put(tick(stamp))
    assert CadenceMarketReader(buffer).market_data_status("INFY", T0) == (prior, None)
    assert buffer.latest_at_or_before("INFY", T0) is prior


def test_snapshot_picks_latest_eligible_symbol_and_same_timestamp_revision():
    buffer = TickBuffer(clock=lambda: T0 + timedelta(seconds=10))
    for value in (tick(-5), tick(-1), tick(-1, price="101.75"),
                  tick(-.5, symbol="INFY", price="999"), tick(3)):
        assert buffer.put(value)
    assert CadenceMarketReader(buffer).market_data_status("AAPL", T0) == (
        tick(-1, price="101.75"), None)
    assert CadenceMarketReader(buffer).market_data_status("MISSING", T0) == (
        None, "Missing Market Data")


def test_snapshot_handles_equivalent_timezone_cutoff():
    buffer = TickBuffer(clock=lambda: T0 + timedelta(seconds=10))
    assert buffer.put(tick()) and buffer.put(tick(3))
    india = timezone(timedelta(hours=5, minutes=30))
    assert CadenceMarketReader(buffer).market_data_status("AAPL", T0.astimezone(india)) == (tick(), None)


@pytest.mark.parametrize("bad", [
    tick(1), replace(tick(), ltp=D("NaN")), replace(tick(), symbol="WRONG"),
    replace(tick(), observed_at=None),
])
def test_history_selector_result_is_revalidated(bad):
    buffer = SimpleNamespace(latest=lambda _: tick(3), latest_at_or_before=lambda *_: bad)
    selected, reason = CadenceMarketReader(buffer).market_data_status("AAPL", T0)
    assert selected is None
    assert reason == ("Future Market Data" if bad == tick(1) else "Invalid Market Data")


def test_cutoff_diagnostic_records_times_not_prices(caplog):
    buffer = TickBuffer(clock=lambda: T0 + timedelta(seconds=3))
    assert buffer.put(tick(-1)) and buffer.put(tick(3, price="987.654321"))
    with caplog.at_level(logging.INFO, logger="quant_ai.cadence"):
        CadenceMarketReader(buffer).market_data_status("AAPL", T0)
    assert "cadence_tick_cutoff" in caplog.text
    assert T0.isoformat() in caplog.text
    assert tick(-1).observed_at.isoformat() in caplog.text
    assert tick(3).observed_at.isoformat() in caplog.text
    assert "987.654321" not in caplog.text



def test_snapshot_eviction_itself_returns_none_not_post_cutoff_tick():
    buffer = TickBuffer(maxlen=1, clock=lambda: T0 + timedelta(seconds=3))
    assert buffer.put(tick(-1)) and buffer.put(tick(3))
    assert buffer.latest_at_or_before("AAPL", T0) is None
    assert buffer.latest_at_or_before("MISSING", T0) is None


def test_snapshot_itself_enforces_symbol_cutoff_and_latest_revision():
    buffer = TickBuffer(clock=lambda: T0 + timedelta(seconds=3))
    values = [tick(-5), tick(-1), tick(-1, price="101.75"),
              tick(-.5, symbol="INFY"), tick(3)]
    for value in values:
        assert buffer.put(value)
    assert buffer.latest_at_or_before("AAPL", T0) is values[2]
    assert buffer.latest_at_or_before("AAPL", T0 + timedelta(seconds=3)) is values[-1]


@pytest.mark.parametrize("bad_latest", [replace(tick(3), symbol="OTHER"),
                                        replace(tick(3), ltp=D("NaN"))])
def test_invalid_latest_is_not_laundered_through_valid_history(bad_latest):
    select = lambda *_: tick()
    buffer = SimpleNamespace(latest=lambda _: bad_latest, latest_at_or_before=select)
    assert CadenceMarketReader(buffer).market_data_status("AAPL", T0) == (None, "Invalid Market Data")


@pytest.mark.parametrize("error", [TypeError, ValueError, AttributeError])
def test_failed_history_read_stays_invalid(error):
    def broken(*_):
        raise error("synthetic source error")
    buffer = SimpleNamespace(latest=lambda _: tick(3), latest_at_or_before=broken)
    assert CadenceMarketReader(buffer).market_data_status("AAPL", T0) == (None, "Invalid Market Data")


def test_noncallable_history_attribute_cannot_admit_future_tick():
    buffer = SimpleNamespace(latest=lambda _: tick(3), latest_at_or_before="not a reader")
    assert CadenceMarketReader(buffer).market_data_status("AAPL", T0) == (None, "Future Market Data")


def test_no_explicit_cutoff_uses_one_current_time(monkeypatch):
    from quant_ai.orchestration import cadence

    class Clock:
        @staticmethod
        def now(tz):
            return T0

    monkeypatch.setattr(cadence, "datetime", Clock)
    buffer = TickBuffer(clock=lambda: T0 + timedelta(seconds=3))
    assert buffer.put(tick()) and buffer.put(tick(3))
    reader = CadenceMarketReader(buffer)
    assert reader.latest_for_consensus("AAPL") == tick()
    assert buffer.latest("AAPL") == tick(3)



def test_snapshot_uses_the_same_lock_as_ingestion():
    buffer = TickBuffer(clock=lambda: T0 + timedelta(seconds=3))
    assert buffer.put(tick()) and buffer.put(tick(3))
    original_lock = buffer._lock
    observed = []

    class ObservedLock:
        def __enter__(self):
            original_lock.acquire()
            observed.append("enter")

        def __exit__(self, *args):
            observed.append("exit")
            original_lock.release()

    buffer._lock = ObservedLock()
    assert buffer.latest_at_or_before("AAPL", T0) == tick()
    assert observed == ["enter", "exit"]
