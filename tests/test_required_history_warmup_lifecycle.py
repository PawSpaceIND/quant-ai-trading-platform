"""Offline runner lifecycle checks; no AWS, credentials or provider access."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from threading import Event, Thread
from types import SimpleNamespace

import pytest
from test_pilot_required_risk_gates import NOW, SYMBOLS, daily, runner


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    import socket
    def denied(*_args, **_kwargs):
        pytest.fail("Unexpected network access in lifecycle verification")
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setenv("TRADING_LIVE_MONEY_ACTIVE", "false")
    for key in ("ZERODHA_API_KEY", "ZERODHA_ACCESS_TOKEN", "PRAMANA_SECTOR_MAP_JSON",
                "PRAMANA_SECTOR_MAP_FILE", "PRAMANA_FOUNDER_DIRECTIVES_JSON",
                "PRAMANA_FOUNDER_DIRECTIVES_FILE"):
        monkeypatch.delenv(key, raising=False)


def test_protection_starts_while_initial_history_request_is_stalled(tmp_path, monkeypatch):
    actual = runner(tmp_path, daily())
    actual.streams = ()
    entered, release, protected = Event(), Event(), Event()
    failures = []
    def warming(_now):
        entered.set()
        assert release.wait(4)
    async def finish():
        return None
    monkeypatch.setattr(actual, "_warm_required_book_history", warming)
    monkeypatch.setattr(actual, "_protect", protected.set)
    monkeypatch.setattr(actual, "_run_aligned_cadence", finish)
    def start():
        try:
            asyncio.run(actual.start())
        except BaseException as error:  # noqa: BLE001 - fail the parent assertion on thread termination
            failures.append(type(error).__name__)
    thread = Thread(target=start)
    thread.start()
    try:
        assert entered.wait(2), "warmup did not start"
        assert protected.wait(0.5), "provider I/O delayed protection startup"
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and failures == []


@pytest.mark.parametrize("boundary", ["provider", "wiring"])
def test_warmup_never_logs_external_exception_details(tmp_path, monkeypatch, caplog, boundary):
    source = daily()
    actual = runner(tmp_path, source)
    marker = "SYNTHETIC_PRIVATE_DIAGNOSTIC_141"
    def failed(*_args):
        raise AttributeError(marker)
    if boundary == "provider":
        monkeypatch.setattr(source, "fetch", failed)
    else:
        class Broken:
            @property
            def book_risk(self):
                raise AttributeError(marker)
        actual.daemon.scheduler.pipeline.runtime = SimpleNamespace(warden=Broken())
    actual._warm_required_book_history(NOW)
    assert marker not in caplog.text
    assert "Traceback" not in caplog.text
    assert "required_book_history_warmup_" in caplog.text


def test_stop_during_warmup_prevents_later_symbol_fetches(tmp_path, monkeypatch):
    source = daily()
    actual = runner(tmp_path, source)
    calls = []
    original = source.fetch
    def stopped(instrument, now):
        calls.append(instrument.symbol)
        actual.request_stop()
        return original(instrument, now)
    monkeypatch.setattr(source, "fetch", stopped)
    actual._warm_required_book_history(NOW)
    assert len(calls) == 1, "warmup continued requests after stop"


def test_stopped_startup_does_not_launch_cadence(tmp_path, monkeypatch):
    actual = runner(tmp_path, daily())
    actual.streams = ()
    calls = []
    monkeypatch.setattr(actual, "_warm_required_book_history", lambda _now: actual.request_stop())
    monkeypatch.setattr(actual, "_protect", lambda: None)
    async def cadence():
        calls.append("cadence")
    monkeypatch.setattr(actual, "_run_aligned_cadence", cadence)
    asyncio.run(actual.start())
    assert calls == [], "cadence launched after a startup stop"


@pytest.mark.parametrize("stop", [False, True])
def test_cadence_uses_post_warmup_clock_and_honors_stop(tmp_path, monkeypatch, stop):
    actual = runner(tmp_path, daily())
    now = [NOW]
    observed = []
    proofs = []
    actual.clock = lambda: now[0]
    async def immediate(_seconds):
        return None
    actual.sleeper = immediate
    def warming(_now):
        now[0] += timedelta(seconds=30)
        if stop:
            actual.request_stop()
    async def once(moment):
        observed.append(moment)
        actual.request_stop()
    async def capture(moment):
        proofs.append(moment)
    monkeypatch.setattr(actual, "_warm_required_book_history", warming)
    monkeypatch.setattr(actual.daemon, "run_once", once)
    monkeypatch.setattr(actual, "_capture_xai_proofs", capture)
    asyncio.run(actual._run_aligned_cadence())
    assert observed == ([] if stop else [NOW + timedelta(seconds=30)])
    assert proofs == observed


def test_warmup_does_not_enable_optional_history(tmp_path):
    source = daily()
    actual = runner(tmp_path, source, required=False)
    touched = []
    class OptionalSource:
        @property
        def fetch(self):
            touched.append("optional_lookup")
            return source.fetch
    actual.daemon.scheduler.pipeline.runtime.warden.book_risk.history_provider.provider = OptionalSource()
    actual._warm_required_book_history(NOW)
    assert source.feed.calls == [] and touched == []


def test_warmup_ignores_unrecognized_history_adapter(tmp_path):
    source = daily()
    actual = runner(tmp_path, source)
    calls = []
    actual.daemon.scheduler.pipeline.runtime.warden.book_risk.history_provider = SimpleNamespace(
        provider=SimpleNamespace(fetch=lambda *_: calls.append("network")),
        instruments={s: object() for s in SYMBOLS},
    )
    actual._warm_required_book_history(NOW)
    assert calls == []


def test_noncallable_fetch_reports_specific_refusal(tmp_path, monkeypatch, caplog):
    source = daily()
    actual = runner(tmp_path, source)
    monkeypatch.setattr(source, "fetch", None)
    actual._warm_required_book_history(NOW)
    assert source.feed.calls == []
    assert [record.getMessage() for record in caplog.records] == [
        "required_book_history_warmup_unavailable"]


def test_missing_identity_is_not_fetched_or_marked_ready(tmp_path, caplog):
    from test_pilot_required_risk_gates import gates
    source = daily()
    actual = runner(tmp_path, source)
    history = actual.daemon.scheduler.pipeline.runtime.warden.book_risk.history_provider
    history.instruments.pop("TCS")
    actual._warm_required_book_history(NOW)
    assert source.feed.calls == sorted(set(SYMBOLS) - {"TCS"})
    assert "required_book_history_warmup_missing_instrument symbol=TCS" in caplog.text
    value = gates(actual)["book_expected_shortfall"]
    assert value["dataReady"] is False and value["coveredSymbols"] == 4


def test_one_failed_symbol_does_not_hide_the_remaining_scope(tmp_path, monkeypatch, caplog):
    from test_pilot_required_risk_gates import gates
    source = daily()
    actual = runner(tmp_path, source)
    fetch = source.fetch
    calls = []
    def fail_first(instrument, now):
        calls.append(instrument.symbol)
        if instrument.symbol == min(SYMBOLS):
            raise OSError("SYNTHETIC_PRIVATE_FAILURE")
        return fetch(instrument, now)
    monkeypatch.setattr(source, "fetch", fail_first)
    actual._warm_required_book_history(NOW)
    assert calls == sorted(SYMBOLS)
    state = gates(actual)["book_expected_shortfall"]
    assert state["coveredSymbols"] == 4 and state["dataReady"] is False
    assert "SYNTHETIC_PRIVATE_FAILURE" not in caplog.text


def test_abstention_is_not_invented_readiness(tmp_path, caplog):
    from test_pilot_required_risk_gates import Feed, gates
    source = daily(Feed(missing=SYMBOLS))
    actual = runner(tmp_path, source)
    actual._warm_required_book_history(NOW)
    assert source.feed.calls == sorted(SYMBOLS)
    state = gates(actual)["book_expected_shortfall"]
    assert state["armed"] is True and state["dataReady"] is False
    assert state["records"] == 0
    assert caplog.text.count("required_book_history_warmup_abstained symbol=") == 5


@pytest.mark.parametrize("startup", [True, False])
def test_history_wait_keeps_event_loop_and_broker_lock_available(tmp_path, monkeypatch, startup):
    actual = runner(tmp_path, daily())
    entered, release, loop_alive = Event(), Event(), Event()
    outcomes = []
    def warming(_now):
        entered.set()
        assert release.wait(4)
    def observer():
        if not entered.wait(2):
            outcomes.append(False)
            release.set()
            return
        lock = actual.daemon.tracker.broker._lock
        acquired = lock.acquire(timeout=1)
        if acquired:
            lock.release()
        outcomes.append(acquired and loop_alive.wait(1))
        release.set()
    async def pulse():
        while not entered.is_set():
            await asyncio.sleep(0.001)
        loop_alive.set()
    async def finish(*_args):
        actual._stop_requested = True
    async def no_proofs(*_args):
        pass
    async def immediate(_seconds):
        pass
    monkeypatch.setattr(actual, "_warm_required_book_history", warming)
    monkeypatch.setattr(actual, "_protect", lambda: None)
    actual.streams = ()
    actual.clock = lambda: NOW
    actual.sleeper = immediate
    if startup:
        monkeypatch.setattr(actual, "_run_aligned_cadence", finish)
    else:
        monkeypatch.setattr(actual.daemon, "run_once", finish)
        monkeypatch.setattr(actual, "_capture_xai_proofs", no_proofs)
    worker = Thread(target=observer)
    async def scenario():
        pulse_task = asyncio.create_task(pulse())
        try:
            await (actual.start() if startup else actual._run_aligned_cadence())
        finally:
            pulse_task.cancel()
            await asyncio.gather(pulse_task, return_exceptions=True)
    worker.start()
    try:
        asyncio.run(scenario())
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert outcomes == [True], "History I/O blocked the event loop or broker lock"


def test_stream_supervisor_runs_before_initial_history_completes(tmp_path, monkeypatch):
    actual = runner(tmp_path, daily())
    started = Event()
    closed = []
    actual.streams = (object(),)
    async def stream(_stream):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.append("stream")
    def warm(_now):
        assert started.wait(1), "Market stream startup waited for history"
    async def finish():
        pass
    monkeypatch.setattr(actual, "_supervise_stream", stream)
    monkeypatch.setattr(actual, "_warm_required_book_history", warm)
    monkeypatch.setattr(actual, "_protect", lambda: None)
    monkeypatch.setattr(actual, "_run_aligned_cadence", finish)
    asyncio.run(actual.start())
    assert closed == ["stream"]


def test_cancelled_startup_cleans_up_and_stops_later_requests(tmp_path, monkeypatch):
    source = daily()
    actual = runner(tmp_path, source)
    actual.clock = lambda: NOW
    actual.streams = (object(),)
    entered, release = Event(), Event()
    calls, cleaned = [], []
    original = source.fetch
    def fetch(instrument, now):
        calls.append(instrument.symbol)
        entered.set()
        assert release.wait(4)
        return original(instrument, now)
    async def stream(_stream):
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.append("supervisor")
    async def shutdown():
        cleaned.append("shutdown")
    monkeypatch.setattr(source, "fetch", fetch)
    monkeypatch.setattr(actual, "_protect", lambda: None)
    monkeypatch.setattr(actual, "_supervise_stream", stream)
    monkeypatch.setattr(actual, "_stop_streams", shutdown)
    async def scenario():
        task = asyncio.create_task(actual.start())
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert actual._stop_requested and actual._protection_stop.is_set()
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())
    assert len(calls) == 1
    assert cleaned == ["supervisor", "shutdown"]


def test_already_stopped_runner_skips_initial_warmup(tmp_path, monkeypatch):
    actual = runner(tmp_path, daily())
    actual.streams = ()
    calls = []
    actual.request_stop()
    monkeypatch.setattr(actual, "_warm_required_book_history", lambda _now: calls.append("warmup"))
    monkeypatch.setattr(actual, "_protect", lambda: None)
    asyncio.run(actual.start())
    assert calls == []


def test_required_source_is_not_replaced_by_regime_source(tmp_path):
    source = daily()
    actual = runner(tmp_path, source)
    regime = daily()
    actual.daemon.scheduler.pipeline.history = regime
    actual._warm_required_book_history(NOW)
    assert source.feed.calls == sorted(SYMBOLS)
    assert regime.feed.calls == []
