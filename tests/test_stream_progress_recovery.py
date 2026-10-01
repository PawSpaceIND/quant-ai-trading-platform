"""Hermetic native callbacks, futures and halted PAPER recovery; no SDK network."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from test_ghost_daemon import FakeDaemon
from test_kite_reconnect import fake_sdk
from test_pilot_closure import INSTRUMENT

from quant_ai.daemon import DaemonRunner
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.execution.risk_state import SQLiteRiskStateStore
from quant_ai.marketdata.feed_watchdog import FeedRecoveryPolicy, progress_issue
from quant_ai.marketdata.ticker_stream import TickBuffer, ZerodhaKiteTicker

NOW = datetime(2026, 9, 30, 5, tzinfo=timezone.utc)
DEFAULT_POLICY = FeedRecoveryPolicy()


def source(monkeypatch):
    reactor, tickers = fake_sdk(monkeypatch)
    now = [NOW]
    buffer = TickBuffer(clock=lambda: now[0])
    stream = ZerodhaKiteTicker('private-key-fixture', 'private-token-fixture', (1,), {1: 'INFY'}, buffer)
    return stream, buffer, now, reactor, tickers


def payload(now, depth=True):
    result = {'instrument_token': 1, 'last_price': 100, 'volume_traded': 10, 'exchange_timestamp': now}
    if depth:
        result['depth'] = {'buy': [{'price': 99, 'quantity': 1}], 'sell': [{'price': 101, 'quantity': 1}]}
    return result


async def drain():
    for _ in range(10):
        await asyncio.sleep(0)


def connect(stream):
    sdk = stream._ticker
    sdk.subscribe = lambda tokens: None
    sdk.set_mode = lambda mode, tokens: None
    sdk.MODE_FULL = 1
    stream._on_connect(sdk, {})
    return sdk


def test_raw_callback_future_buffer_and_subscription_are_separate_redacted_fields(monkeypatch):
    stream, buffer, now, _, _ = source(monkeypatch)
    async def run():
        await stream.start()
        sdk = connect(stream)
        stream._on_raw_message(sdk, b'private raw payload fixture', True)
        stream._on_ticks(sdk, [payload(now[0])])
        assert stream.progress.snapshot()['pendingFutures'] == 2
        await drain()
        data = buffer.integrity()
        progress = data['zerodhaIngestion']
        assert data['accepted'] == 1
        assert data['lastBufferWriteAt'] == NOW.isoformat()
        assert data['lastAcceptedSourceAt'] == NOW.isoformat()
        assert progress['lastRawFrameAt'] == progress['lastTickCallbackAt'] == NOW.isoformat()
        assert progress['counts']['futuresCompleted'] == 2
        assert progress['pendingFutures'] == 0
        assert progress['subscriptionSendState'] == 'sent_unconfirmed'
        assert not progress['subscriptionAcknowledged']
        serialized = json.dumps(data)
        assert 'private' not in serialized and 'payload fixture' not in serialized
        await stream.stop()
    asyncio.run(run())


def test_callback_failure_is_observed_without_payload_or_exception_text(monkeypatch):
    stream, buffer, now, _, _ = source(monkeypatch)
    async def fail(tick):
        raise RuntimeError('private provider diagnostic fixture')
    stream.on_tick = fail
    async def run():
        await stream.start()
        stream._on_ticks(stream._ticker, [payload(now[0], depth=False)])
        await drain()
        data = buffer.integrity()
        assert data['accepted'] == 0
        assert data['zerodhaIngestion']['currentGenerationFutureFailures'] == 1
        assert data['zerodhaIngestion']['pendingFutures'] == 0
        now[0] += timedelta(minutes=5)
        assert progress_issue(stream.progress.snapshot(), now[0], NOW, FeedRecoveryPolicy()) == 'consumer_future_failed_manual_recovery'
        assert 'private' not in json.dumps(data)
        await stream.stop()
    asyncio.run(run())


def test_pending_consumer_is_visible_and_cannot_authorize_socket_recovery(monkeypatch):
    stream, _, now, _, _ = source(monkeypatch)
    async def run():
        release = asyncio.Event()
        async def blocked(tick):
            await release.wait()
        stream.on_tick = blocked
        await stream.start()
        stream._on_ticks(stream._ticker, [payload(now[0], depth=False)])
        await drain()
        now[0] += timedelta(minutes=5)
        evidence = stream.progress.snapshot()
        assert evidence['pendingFutures'] == 1 and evidence['pendingSince'] == NOW.isoformat()
        assert progress_issue(evidence, now[0], NOW, FeedRecoveryPolicy()) == 'consumer_future_pending_no_recovery'
        release.set()
        await drain()
        assert stream.progress.snapshot()['pendingFutures'] == 0
        await stream.stop()
    asyncio.run(run())


def test_listener_failure_distinguishes_written_buffer_from_failed_consumer(monkeypatch):
    stream, buffer, now, _, _ = source(monkeypatch)
    def listener(tick):
        raise RuntimeError('private listener failure fixture')
    buffer.subscribe(listener)
    async def run():
        await stream.start()
        stream._on_ticks(stream._ticker, [payload(now[0], depth=False)])
        await drain()
        evidence = buffer.integrity()
        assert evidence['accepted'] == 1 and evidence['lastBufferWriteAt'] == NOW.isoformat()
        assert evidence['zerodhaIngestion']['currentGenerationFutureFailures'] == 1
        assert 'private' not in json.dumps(evidence)
        await stream.stop()
    asyncio.run(run())


def test_old_sdk_callbacks_do_not_refresh_new_generation_or_cancel_native_retry(monkeypatch):
    stream, _, now, _, _ = source(monkeypatch)
    async def run():
        await stream.start()
        old = stream._ticker
        await stream.stop()
        now[0] += timedelta(minutes=1)
        await stream.start()
        stream._on_raw_message(old, b'ignored', True)
        stream._on_ticks(old, [payload(now[0])])
        stream._on_close(old, 1006, 'ignored')
        evidence = stream.progress.snapshot()
        assert evidence['connectionGeneration'] == 2
        assert 'lastRawFrameAt' not in evidence and not evidence['nativeRetryActive']
        assert evidence['counts']['ignoredOldCallbacks'] == 3
        stream._on_reconnect(stream._ticker, 1)
        now[0] += timedelta(minutes=5)
        assert progress_issue(stream.progress.snapshot(), now[0], NOW, FeedRecoveryPolicy()) == 'native_retry_active'
        await stream.stop()
    asyncio.run(run())


def runner(tmp_path, monkeypatch, *, policy=DEFAULT_POLICY):
    monkeypatch.setenv('TRADING_LIVE_MONEY_ACTIVE', 'false')
    stream, buffer, now, reactor, tickers = source(monkeypatch)
    daemon = FakeDaemon()
    broker = PaperBrokerService(tmp_path / 'paper.sqlite')
    risk = SQLiteRiskStateStore(tmp_path / 'paper.sqlite', connection=broker._connection, lock=broker._lock)
    daemon.tracker = SimpleNamespace(broker=broker, risk_state=risk)
    daemon.tenant_id = 'pilot'
    daemon.telemetry = object()
    daemon.instruments = (INSTRUMENT,)
    daemon.kill_switch = SimpleNamespace(engaged=True, reason='protection_unreachable:INFY')
    risk.set_kill_switch('pilot', True, daemon.kill_switch.reason)
    daemon.prices_expected = lambda symbol, at: True
    async def no_wait(delay):
        await asyncio.sleep(0)
    result = DaemonRunner(daemon, (stream,), clock=lambda: now[0],
                          log_path=tmp_path / 'synthetic.log', feed_recovery_policy=policy, sleeper=no_wait)
    return result, stream, buffer, now, reactor, tickers, broker


def test_default_does_not_create_watchdog_or_recover_silent_stream(tmp_path, monkeypatch):
    observed, stream, _, now, _, tickers, broker = runner(tmp_path, monkeypatch, policy=None)
    async def run():
        await stream.start()
        active = {stream: NOW}
        now[0] += timedelta(minutes=5)
        await observed._check_silent_feeds(active)
        assert len(tickers) == 1 and stream.progress.snapshot()['watchdogState'] == 'disabled'
        await stream.stop()
    try:
        asyncio.run(run())
    finally:
        broker.close()


def test_one_halted_recovery_uses_supervisor_thread_handoff_without_resume(tmp_path, monkeypatch):
    observed, stream, _, now, reactor, tickers, broker = runner(tmp_path, monkeypatch)
    async def run():
        task = asyncio.create_task(observed._supervise_stream(stream))
        await drain()
        connect(stream)
        active = {stream: NOW}
        now[0] += timedelta(minutes=5)
        await observed._check_silent_feeds(active)
        # The supervisor journals through a worker before replacing the socket.
        async def replaced():
            while len(tickers) != 2:
                await asyncio.sleep(0.001)
        await asyncio.wait_for(replaced(), timeout=2)
        assert len(tickers) == 2 and tickers[0].closes == 1
        assert reactor.handed_over[:2] == ['close', 'connect']
        assert observed.daemon.kill_switch.engaged
        assert observed.daemon.tracker.risk_state.kill_switch_state('pilot') == (True, 'protection_unreachable:INFY')
        now[0] += timedelta(minutes=5)
        await observed._check_silent_feeds(active)
        assert stream.progress.snapshot()['watchdogState'] == 'recovery_limit_requires_operator'
        assert observed._feed_recoveries == 1
        observed.request_stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    try:
        asyncio.run(run())
    finally:
        broker.close()


@pytest.mark.parametrize('case', ['closed', 'auction', 'no_durable_halt', 'mismatched_halt', 'retry', 'pending', 'failed'])
def test_nonrecoverable_states_do_not_replace_socket(tmp_path, monkeypatch, case):
    observed, stream, _, now, _, tickers, broker = runner(tmp_path, monkeypatch)
    async def run():
        await stream.start()
        connect(stream)
        active = {stream: NOW}
        now[0] += timedelta(minutes=5)
        if case in ('closed', 'auction'): observed.daemon.prices_expected = lambda symbol, at: False
        if case == 'no_durable_halt': observed.daemon.tracker.risk_state.set_kill_switch('pilot', False, None)
        if case == 'mismatched_halt': observed.daemon.kill_switch.reason = 'different'
        if case == 'retry': stream.progress.retrying(True)
        if case == 'pending': stream.progress.queued()
        if case == 'failed': stream.progress.finished(stream.progress.queued(), failed=True)
        await observed._check_silent_feeds(active)
        assert observed._feed_recoveries == 0 and len(tickers) == 1
        assert stream.progress.snapshot()['watchdogState'] != 'bounded_socket_recovery_requested'
        await stream.stop()
    try:
        asyncio.run(run())
    finally:
        broker.close()


@pytest.mark.parametrize('seconds', [0, 60, 901, True, '180'])
def test_invalid_recovery_policy_refused(seconds):
    with pytest.raises(ValueError):
        FeedRecoveryPolicy(silent_seconds=seconds)


def test_read_only_snapshot_is_independent_and_bounded(monkeypatch):
    stream, _, _, _, _ = source(monkeypatch)
    stream.progress.start()
    for _ in range(1000):
        stream._on_raw_message(None, b'secret fixture', True)
    first = stream.progress.snapshot()
    first['counts'].clear()
    second = stream.progress.snapshot()
    assert second['counts']['rawFrameCallbacks'] == 1000
    assert len(second['counts']) == 1
    assert 'secret' not in json.dumps(second)


@pytest.mark.parametrize('change', ['future_timestamp', 'bad_pending', 'bad_retry', 'missing_start'])
def test_unknown_progress_never_authorizes_recovery(monkeypatch, change):
    stream, _, now, _, _ = source(monkeypatch)
    stream.progress.start()
    now[0] += timedelta(minutes=5)
    evidence = stream.progress.snapshot()
    if change == 'future_timestamp': evidence['lastRawFrameAt'] = (now[0] + timedelta(seconds=1)).isoformat()
    if change == 'bad_pending': evidence['pendingFutures'] = True
    if change == 'bad_retry': evidence['nativeRetryActive'] = 'unknown'
    if change == 'missing_start': del evidence['generationStartedAt']
    assert progress_issue(evidence, now[0], NOW, DEFAULT_POLICY) == 'progress_unknown'


def test_raw_heartbeat_progress_does_not_become_buffer_freshness(monkeypatch):
    stream, buffer, now, _, _ = source(monkeypatch)
    stream.progress.start()
    stream.progress.note('connected')
    now[0] += timedelta(minutes=5)
    stream._on_raw_message(None, b'heartbeat', True)
    assert buffer.integrity()['accepted'] == 0
    assert buffer.integrity()['lastBufferWriteAt'] is None
    assert progress_issue(stream.progress.snapshot(), now[0], NOW, DEFAULT_POLICY) == 'raw_frames_without_tick_callbacks'


def test_opt_in_refuses_missing_monitoring_and_subscription_mismatch(tmp_path, monkeypatch):
    observed, stream, _, _now, _, _, broker = runner(tmp_path, monkeypatch)
    try:
        stream.symbol_by_token = {1: 'UNKNOWN'}
        with pytest.raises(ValueError):
            DaemonRunner(observed.daemon, (stream,), feed_recovery_policy=DEFAULT_POLICY)
        stream.symbol_by_token = {1: 'INFY'}
        observed.daemon.telemetry = None
        with pytest.raises(ValueError, match='monitored_paper'):
            DaemonRunner(observed.daemon, (stream,), feed_recovery_policy=DEFAULT_POLICY)
    finally:
        broker.close()
