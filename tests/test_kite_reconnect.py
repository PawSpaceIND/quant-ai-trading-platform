"""A dropped Kite connection is left to KiteTicker's own retry, and a replacement is thread-safe.

24 September 2026: Kite dropped the websocket at 07:06 IST (close 1006). The supervisor
stopped the ticker, which cancels KiteTicker's own retry, then connected a new ticker from
a worker thread while KiteTicker's Twisted reactor was already running in another. Twisted
is not thread-safe; the new connection never opened and never failed, so no error arrived
and no tick either, until the unpriced-stop halt fired at 09:17. These tests stand in for
the SDK and its reactor and replay that sequence.
"""
from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

from test_ghost_daemon import FakeDaemon

from quant_ai.daemon import DaemonRunner, ReconnectPolicy
from quant_ai.marketdata import ticker_stream
from quant_ai.marketdata.ticker_stream import TickerGaveUp, ZerodhaKiteTicker


class FakeReactor:
    """Records which thread asked for work, as Twisted's callFromThread hands it over."""

    def __init__(self) -> None:
        self.running = False
        self.handed_over: list[str] = []

    def callFromThread(self, function, *args, **kwargs):
        self.handed_over.append(function.__name__)
        function(*args, **kwargs)


def fake_sdk(monkeypatch):
    reactor = FakeReactor()
    tickers: list = []

    class FakeKiteTicker:
        def __init__(self, api_key, access_token) -> None:
            self.connects: list[str] = []
            self.closes = 0
            tickers.append(self)

        def connect(self, threaded=False) -> None:
            self.connects.append(threading.current_thread().name)
            reactor.running = True    # the first connect starts the one reactor

        def close(self) -> None:
            self.closes += 1

    modules = {"kiteconnect": SimpleNamespace(KiteTicker=FakeKiteTicker),
               "kiteconnect.ticker": SimpleNamespace(reactor=reactor)}
    monkeypatch.setattr(ticker_stream, "import_module", lambda name: modules[name])
    return reactor, tickers


def stream():
    return ZerodhaKiteTicker("synthetic-key", "synthetic-token", [1], {1: "COALINDIA"})


def test_a_routine_drop_is_recoverable_and_a_give_up_is_not(monkeypatch):
    fake_sdk(monkeypatch)
    kite = stream()
    assert kite.recovers_in_place(ConnectionError("KiteTicker closed 1006: peer dropped"))
    assert not kite.recovers_in_place(TickerGaveUp("maximum retries"))


def test_the_first_connect_starts_the_reactor_from_a_worker_thread(monkeypatch):
    reactor, tickers = fake_sdk(monkeypatch)
    kite = stream()
    asyncio.run(kite.start())
    assert reactor.handed_over == []
    assert tickers[0].connects and tickers[0].connects[0] != threading.main_thread().name
    # KiteTicker only reports its give-up through this callback; unwired, a dead
    # reconnect loop would never reach the supervisor.
    assert tickers[0].on_noreconnect == kite._on_noreconnect


def test_a_replacement_connection_is_handed_to_the_running_reactor(monkeypatch):
    reactor, tickers = fake_sdk(monkeypatch)
    kite = stream()

    async def scenario():
        await kite.start()
        await kite.stop()
        await kite.start()

    asyncio.run(scenario())
    # Once the reactor runs, close and connect go through callFromThread: never a
    # foreign-thread call into Twisted, which is what left the replacement unopened.
    assert reactor.handed_over == ["close", "connect"]
    assert len(tickers) == 2 and tickers[0].closes == 1 and len(tickers[1].connects) == 1


def test_the_supervisor_leaves_kite_reconnecting_and_replaces_it_only_after_a_give_up(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_LIVE_MONEY_ACTIVE", "false")
    reactor, tickers = fake_sdk(monkeypatch)
    kite = stream()
    runner: DaemonRunner

    async def sleeper(delay: float) -> None:
        runner.request_stop()
        await asyncio.sleep(0)

    runner = DaemonRunner(FakeDaemon(), (kite,), reconnect=ReconnectPolicy(1, 8, 2),
                          log_path=tmp_path / "ghost.log", sleeper=sleeper)

    async def scenario():
        task = asyncio.create_task(runner._supervise_stream(kite))
        await asyncio.sleep(0.05)
        kite._on_close(None, 1006, "connection was closed uncleanly")
        await asyncio.sleep(0.05)
        # The drop is journaled, and nothing touched the ticker: KiteTicker's own
        # retry is still armed. This is the step that used to call close().
        assert tickers[0].closes == 0 and len(tickers) == 1
        kite._on_noreconnect(None)
        await asyncio.wait_for(task, 2)

    asyncio.run(scenario())
    assert tickers[0].closes == 1 and reactor.handed_over == ["close"]
    events = [json.loads(line) for line in (tmp_path / "ghost.log").read_text().splitlines()]
    assert [event["event"] for event in events] == ["websocket_disconnected"] * 2
    assert "1006" in events[0]["error"] and "maximum retries" in events[1]["error"]

