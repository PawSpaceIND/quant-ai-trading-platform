from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from quant_ai.agents.contracts import AgentDomain, Stance
from quant_ai.agents.specialists import SpecialistAgent
from quant_ai.marketdata.ticker_stream import IBKRAsyncTicker, LiveTick, TickBuffer
from quant_ai.orchestration.cadence import CadenceMarketReader


def _tick(symbol: str = "NIFTY") -> LiveTick:
    return LiveTick(
        symbol=symbol,
        ltp=Decimal("25000.25"),
        volume=Decimal("12345"),
        bid=Decimal("25000.20"),
        ask=Decimal("25000.30"),
        observed_at=datetime.now(timezone.utc),
        source="test",
    )


def test_tick_buffer_returns_latest_and_spread() -> None:
    buffer = TickBuffer(maxlen=2)
    first = _tick()
    second = LiveTick(
        symbol="NIFTY",
        ltp=Decimal("25001"),
        volume=Decimal("13000"),
        bid=Decimal("25000.90"),
        ask=Decimal("25001.10"),
        observed_at=datetime.now(timezone.utc),
        source="test",
    )
    buffer.put(first)
    buffer.put(second)

    assert buffer.latest("NIFTY") == second
    assert second.spread == Decimal("0.20")
    assert buffer.snapshot()["NIFTY"].ltp == Decimal("25001")


def test_cadence_reader_rejects_stale_ticks() -> None:
    buffer = TickBuffer()
    now = datetime.now(timezone.utc)
    stale = LiveTick(
        symbol="AAPL",
        ltp=Decimal("220"),
        volume=Decimal("10"),
        bid=Decimal("219.9"),
        ask=Decimal("220.1"),
        observed_at=now - timedelta(minutes=5),
        source="test",
    )
    buffer.put(stale)
    reader = CadenceMarketReader(buffer, max_tick_age=timedelta(minutes=2))

    assert reader.latest_for_consensus("AAPL", now=now) is None


def test_technical_specialist_injects_live_market_context() -> None:
    agent = SpecialistAgent("technical-quant", AgentDomain.TECHNICAL)
    evidence = agent.publish(
        subject="NIFTY",
        stance=Stance.BUY,
        confidence=Decimal("0.8"),
        expected_return=Decimal("0.02"),
        expected_risk=Decimal("0.01"),
        rationale=("momentum_positive",),
        observed_at=datetime.now(timezone.utc),
        source_freshness_seconds=1,
        market_tick=_tick(),
    )

    assert "live_ltp=25000.25" in evidence.rationale
    assert "live_volume=12345" in evidence.rationale
    assert "live_bid_ask_spread=0.10" in evidence.rationale


class _FakeEvent:
    def __init__(self) -> None:
        self.handlers: list[object] = []

    def __iadd__(self, handler: object) -> "_FakeEvent":
        self.handlers.append(handler)
        return self


class _FakeIB:
    def __init__(self) -> None:
        self.quote = SimpleNamespace(
            contract=SimpleNamespace(localSymbol="AAPL"),
            last=220.5,
            volume=1000,
            bid=220.4,
            ask=220.6,
            updateEvent=_FakeEvent(),
        )
        self.cancelled: list[object] = []

    def reqMktData(self, contract: object, *args: object) -> object:
        return self.quote

    def cancelMktData(self, contract: object) -> None:
        self.cancelled.append(contract)


def test_ibkr_stream_updates_memory_without_orders() -> None:
    async def scenario() -> None:
        contract = SimpleNamespace(localSymbol="AAPL")
        ib = _FakeIB()
        stream = IBKRAsyncTicker(ib, [contract])

        await stream.start()
        assert len(ib.quote.updateEvent.handlers) == 1
        ib.quote.updateEvent.handlers[0](ib.quote)
        latest = stream.buffer.latest("AAPL")

        assert latest is not None
        assert latest.ltp == Decimal("220.5")
        assert latest.spread == Decimal("0.2")

        await stream.stop()
        assert ib.cancelled == [contract]

    asyncio.run(scenario())
