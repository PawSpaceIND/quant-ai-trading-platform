from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from quant_ai.daemon import DaemonRunner, ReconnectPolicy, build_ghost_runner
from quant_ai.marketdata.ticker_stream import AbstractTickerStream


class FakeStream(AbstractTickerStream):
    def __init__(self, fail_first: bool = False) -> None:
        super().__init__()
        self.fail_first = fail_first
        self.starts = 0
        self.stops = 0

    async def start(self) -> None:
        self.starts += 1
        if self.fail_first and self.starts == 1:
            raise ConnectionError("simulated drop")
        if self.starts == 2:
            await self.on_connection_error(ConnectionError("simulated websocket close"))

    async def stop(self) -> None:
        self.stops += 1


class FakeDaemon:
    def __init__(self) -> None:
        self.calls: list[datetime] = []
        self.logger = FakeLogger()
        self.tracker = SimpleNamespace(broker=SimpleNamespace(flush=lambda: None))
        self.scheduler = SimpleNamespace(
            pipeline=SimpleNamespace(runtime=SimpleNamespace(xai_logger=self.logger))
        )

    def request_stop(self) -> None:
        return None

    async def run_once(self, now: datetime) -> None:
        self.calls.append(now)
        self.logger.items.append({"decision_id": "proof-1"})


class FakeLogger:
    def __init__(self) -> None:
        self.items: list[object] = []

    def traces(self) -> tuple[object, ...]:
        return tuple(self.items)

    def to_json(self, trace: object) -> str:
        return json.dumps(trace)


def test_ghost_daemon_rejects_live_money(monkeypatch) -> None:
    monkeypatch.setenv("TRADING_LIVE_MONEY_ACTIVE", "true")
    with pytest.raises(RuntimeError, match="refuses to start"):
        DaemonRunner(FakeDaemon(), ())


def test_supervisor_reconnects_after_connection_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_LIVE_MONEY_ACTIVE", "false")
    stream = FakeStream(fail_first=True)
    daemon = FakeDaemon()
    sleeps: list[float] = []
    runner: DaemonRunner

    async def sleeper(delay: float) -> None:
        sleeps.append(delay)
        if stream.starts >= 2:
            runner.request_stop()
        await asyncio.sleep(0)

    runner = DaemonRunner(
        daemon,
        (stream,),
        reconnect=ReconnectPolicy(1, 8, 2),
        log_path=tmp_path / "ghost.log",
        sleeper=sleeper,
    )
    asyncio.run(runner._supervise_stream(stream))

    assert stream.starts == 2
    assert stream.stops >= 2
    assert sleeps == [1, 1]
    events = [json.loads(line)["event"] for line in (tmp_path / "ghost.log").read_text().splitlines()]
    assert events == ["websocket_connect_failed", "websocket_disconnected"]


def test_cadence_aligns_to_next_ten_minute_boundary(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_LIVE_MONEY_ACTIVE", "false")
    daemon = FakeDaemon()
    moments = iter(
        (
            datetime(2026, 9, 12, 10, 3, 30, tzinfo=timezone.utc),
            datetime(2026, 9, 12, 10, 10, 0, tzinfo=timezone.utc),
        )
    )
    delays: list[float] = []
    runner: DaemonRunner

    async def sleeper(delay: float) -> None:
        delays.append(delay)
        await asyncio.sleep(0)

    def clock() -> datetime:
        try:
            return next(moments)
        except StopIteration:
            runner.request_stop()
            return datetime(2026, 9, 12, 10, 10, tzinfo=timezone.utc)

    runner = DaemonRunner(daemon, (), log_path=tmp_path / "ghost.log", clock=clock, sleeper=sleeper)
    asyncio.run(runner._run_aligned_cadence())

    assert delays[0] == 390
    assert daemon.calls == [datetime(2026, 9, 12, 10, 10, tzinfo=timezone.utc)]
    payload = json.loads((tmp_path / "ghost.log").read_text().strip())
    assert payload["event"] == "xai_proof"
    assert payload["proof"]["decision_id"] == "proof-1"


def test_build_ghost_runner_wires_paper_broker_and_both_streams(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_LIVE_MONEY_ACTIVE", "false")
    runner = build_ghost_runner(
        zerodha_api_key="test-key",
        zerodha_access_token="test-token",
        zerodha_instrument_tokens=(256265,),
        zerodha_symbol_by_token={256265: "NIFTY"},
        ib_client=SimpleNamespace(),
        ib_contracts=(),
        database=tmp_path / "paper.db",
        log_path=tmp_path / "ghost.log",
    )

    assert type(runner.daemon.tracker.broker).__name__ == "PaperBrokerService"
    assert [type(stream).__name__ for stream in runner.streams] == [
        "ZerodhaKiteTicker",
        "IBKRAsyncTicker",
    ]
    assert runner.cadence.total_seconds() == 600
