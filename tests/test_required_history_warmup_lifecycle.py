"""Offline runner lifecycle checks; no AWS, credentials or provider access."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from threading import Event, Thread
from types import SimpleNamespace

import pytest
from test_pilot_required_risk_gates import NOW, daily, runner


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
        except BaseException as error:  # noqa: BLE001 - capture thread lifecycle failures
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
