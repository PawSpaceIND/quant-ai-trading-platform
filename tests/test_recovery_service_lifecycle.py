"""Real loopback HTTP with disposable paper stores; never start an SDK feed."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from test_institutional_swarm_bridge import BridgeHarness

from quant_ai.daemon import DaemonRunner
from quant_ai.operations.institutional_operator import InstitutionalRecoveryOperations
from quant_ai.security.persistent_api_keys import PersistentApiKeyRegistry


def test_existing_runner_can_cohost_only_its_exact_recovery_runtime(tmp_path):
    from quant_ai.operations.recovery_service import RecoveryServiceHost
    h = BridgeHarness(tmp_path)
    keys = PersistentApiKeyRegistry(tmp_path / "keys.sqlite", create=True)
    operations = InstitutionalRecoveryOperations(h.runtime, tmp_path / "audit.sqlite")
    try:
        raw, _ = keys.issue("tenant", scopes=("institutional.recovery.read",),
                            expires_at=datetime.now(timezone.utc)+timedelta(hours=1))
        ticks = []
        daemon = SimpleNamespace(scheduler=SimpleNamespace(pipeline=SimpleNamespace(runtime=h.runtime)),
            tenant_id="tenant", tracker=SimpleNamespace(broker=h.broker), kill_switch=h.runtime.kill_switch,
            protection_tick=lambda _: ticks.append(1), request_stop=lambda:None,
            engage_kill_switch=h.runtime.kill_switch.engage)
        runner = DaemonRunner(daemon, (), protection_interval=.01, log_path=tmp_path / "runner.log")
        service = RecoveryServiceHost(daemon=daemon, keys=keys, operations=operations, port=0)
        runner.attach_recovery_service(service)
        async def scenario():
            task = asyncio.create_task(runner.start())
            try:
                for _ in range(300):
                    if service.status == "serving": break
                    await asyncio.sleep(.01)
                assert service.status == "serving"
                async with httpx.AsyncClient() as client:
                    response = await client.get(service.origin + "/health")
                    assert response.status_code == 200
                    assert response.json()["trading_routes_available"] is False
                    assert (await client.post(service.origin+"/v1/paper/trades",
                        headers={"X-API-Key":raw},json={})).status_code == 404
                assert ticks
            finally:
                runner.request_stop()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            assert service.status == "stopped"
            assert keys.db.execute("SELECT count(*) FROM operator_credential_grants").fetchone()[0] == 1
            assert operations._records() == []
        asyncio.run(scenario())
    finally:
        operations.close(); keys.close(); h.close()


class LocalHost:
    def __init__(self, root, **options):
        from quant_ai.operations.recovery_service import RecoveryServiceHost
        self.h = BridgeHarness(root)
        self.keys = PersistentApiKeyRegistry(root / "keys.sqlite", create=True)
        self.operations = InstitutionalRecoveryOperations(self.h.runtime, root / "audit.sqlite")
        self.raw, _ = self.keys.issue("tenant", scopes=("institutional.recovery.read", "institutional.recovery.apply"),
            expires_at=datetime.now(timezone.utc)+timedelta(hours=1))
        self.ticks = []
        self.daemon = SimpleNamespace(scheduler=SimpleNamespace(pipeline=SimpleNamespace(runtime=self.h.runtime)),
            tenant_id="tenant", tracker=SimpleNamespace(broker=self.h.broker), kill_switch=self.h.runtime.kill_switch,
            protection_tick=lambda _: self.ticks.append(1), request_stop=lambda:None,
            engage_kill_switch=self.h.runtime.kill_switch.engage)
        self.runner = DaemonRunner(self.daemon, (), protection_interval=.01, log_path=root / "runner.log")
        self.host = RecoveryServiceHost(daemon=self.daemon, keys=self.keys, operations=self.operations, **options)
        self.runner.attach_recovery_service(self.host)

    def close(self):
        self.operations.close(); self.keys.close(); self.h.close()


async def wait_for(predicate):
    for _ in range(400):
        if predicate(): return
        await asyncio.sleep(.01)
    raise AssertionError("Synthetic lifecycle did not reach expected state")


async def stop_runner(f, task):
    f.runner.request_stop()
    task.cancel()
    results = await asyncio.gather(task, return_exceptions=True)
    assert all(value is None or isinstance(value, asyncio.CancelledError) for value in results), results


@pytest.mark.parametrize("host", ["0.0.0.0", "localhost", "192.0.2.4", "example.com", "", [], None])
def test_listener_cannot_bind_an_unreviewed_nonloopback_host(tmp_path, host):
    from quant_ai.operations.recovery_service import RecoveryServiceHost
    f = LocalHost(tmp_path, port=0)
    try:
        with pytest.raises(ValueError, match="configuration_invalid"):
            RecoveryServiceHost(daemon=f.daemon, keys=f.keys, operations=f.operations, host=host)
        assert f.host.status == "not_started" and f.host.origin is None
        assert f.operations._records() == []
    finally: f.close()


@pytest.mark.parametrize("port", [True, -1, 65536, "8765", None])
def test_invalid_port_never_opens_a_listener(tmp_path, port):
    from quant_ai.operations.recovery_service import RecoveryServiceHost
    f = LocalHost(tmp_path, port=0)
    try:
        with pytest.raises(ValueError):
            RecoveryServiceHost(daemon=f.daemon, keys=f.keys, operations=f.operations, port=port)
    finally: f.close()


@pytest.mark.parametrize("timeout", [True, 0, -1, 31, float("nan"), float("inf"), "10"])
def test_invalid_startup_deadline_refuses(tmp_path, timeout):
    from quant_ai.operations.recovery_service import RecoveryServiceHost
    f = LocalHost(tmp_path, port=0)
    try:
        with pytest.raises(ValueError):
            RecoveryServiceHost(daemon=f.daemon, keys=f.keys, operations=f.operations, startup_timeout=timeout)
    finally: f.close()


@pytest.mark.parametrize("fault", ["runtime", "tenant", "kill_switch", "keys", "operation"])
def test_swapped_borrowed_selection_cannot_be_served(tmp_path, fault):
    f = LocalHost(tmp_path, port=0)
    try:
        if fault == "runtime": f.daemon.scheduler.pipeline.runtime = object()
        elif fault == "tenant": f.daemon.tenant_id = "other"
        elif fault == "kill_switch":
            from quant_ai.operations.kill_switch import KillSwitch
            f.daemon.kill_switch = KillSwitch()
        elif fault == "keys": f.host.keys = object()
        else: f.host.operations = object()
        asyncio.run(f.host.start())
        assert f.host.status == "failed" and f.host.origin is None
        assert f.host._socket is None
        assert f.daemon.kill_switch.engaged
        assert f.operations._records() == []
    finally: f.close()


@pytest.mark.parametrize("preexisting_halt", [False, True])
def test_occupied_port_holds_entries_but_does_not_stop_protection_or_runner(tmp_path, preexisting_halt):
    import socket
    occupied = socket.socket()
    occupied.bind(("127.0.0.1", 0)); occupied.listen(1)
    f = LocalHost(tmp_path, port=occupied.getsockname()[1])
    if preexisting_halt: f.daemon.kill_switch.engage("existing operator halt")
    async def scenario():
        task = asyncio.create_task(f.runner.start())
        try:
            await wait_for(lambda:f.host.status == "failed")
            before = len(f.ticks)
            await asyncio.sleep(.05)
            assert len(f.ticks) > before and not task.done()
            assert f.daemon.kill_switch.engaged
            assert f.daemon.kill_switch.reason == ("existing operator halt" if preexisting_halt else "recovery_listener_unavailable")
            assert f.operations._records() == []
        finally: await stop_runner(f, task)
    try: asyncio.run(scenario())
    finally: occupied.close(); f.close()


def test_unexpected_listener_exit_latches_a_hold_without_automatic_restart(tmp_path):
    f = LocalHost(tmp_path, port=0)
    async def scenario():
        task = asyncio.create_task(f.runner.start())
        try:
            await wait_for(lambda:f.host.status == "serving")
            original_task = f.host._task
            f.host._server.should_exit = True
            await wait_for(lambda:f.host.status == "failed")
            ticks = len(f.ticks)
            await asyncio.sleep(.05)
            assert len(f.ticks) > ticks and not task.done()
            assert f.host._task is original_task
            assert f.daemon.kill_switch.engaged and f.host.origin is None
        finally: await stop_runner(f, task)
    try: asyncio.run(scenario())
    finally: f.close()


def test_stop_drains_admitted_bookkeeping_while_protection_remains_active(tmp_path, monkeypatch):
    from threading import Event

    from test_institutional_bridge_recovery import interrupted_fill
    f = LocalHost(tmp_path, port=0)
    entered, release = Event(), Event()
    pid = interrupted_fill(f.h, monkeypatch)
    context = f.h.programs.get(pid).context_sha256
    original = f.h.runtime.reconcile_program
    def blocking(*args, **kwargs):
        entered.set()
        assert release.wait(5), "Synthetic drain released too late"
        return original(*args, **kwargs)
    monkeypatch.setattr(f.h.runtime, "reconcile_program", blocking)
    async def scenario():
        task = asyncio.create_task(f.runner.start())
        request = None
        try:
            await wait_for(lambda:f.host.status == "serving")
            async with httpx.AsyncClient(timeout=10) as client:
                request = asyncio.create_task(client.post(f.host.origin+f"/v1/institutional/programs/{pid}/recovery",
                    headers={"X-API-Key":f.raw},json={"request_id":"drain-request", "expected_context_sha256":context,
                        "confirmation":"reconcile_recorded_paper_bookkeeping_only"}))
                await wait_for(entered.is_set)
                f.runner.request_stop(); task.cancel()
                await wait_for(lambda:f.host.status == "draining")
                ticks = len(f.ticks)
                await asyncio.sleep(.08)
                assert len(f.ticks) > ticks and not task.done()
                assert f.host.origin is None and not f.runner._protection_stop.is_set()
                release.set()
                response = await request
                assert response.status_code == 200
                assert response.json()["result"]["program_state"] == "COMPLETE"
                await asyncio.gather(task, return_exceptions=True)
                assert f.host.status == "stopped" and f.runner._protection_stop.is_set()
                assert len(f.h.broker.ledger_entries("tenant")) == 1
                assert len(f.operations._records()) == 2
                assert f.keys.authenticate(f.raw) is not None
        finally:
            release.set()
            await stop_runner(f, task)
            if request is not None: await asyncio.gather(request, return_exceptions=True)
    try: asyncio.run(scenario())
    finally: f.close()


def test_runner_default_does_not_start_any_recovery_listener(tmp_path, monkeypatch):
    import socket
    f = LocalHost(tmp_path, port=0)
    runner = DaemonRunner(f.daemon, (), log_path=tmp_path / "default.log")
    async def scenario():
        def forbidden(*args, **kwargs): raise AssertionError("Unselected recovery cannot bind a socket")
        monkeypatch.setattr(socket.socket, "bind", forbidden)
        task = asyncio.create_task(runner.start())
        try:
            await asyncio.sleep(.04)
            assert not task.done()
            assert runner._recovery_service is None
        finally:
            runner.request_stop(); task.cancel(); await asyncio.gather(task, return_exceptions=True)
    try: asyncio.run(scenario())
    finally: f.close()


def test_service_selection_is_one_shot_and_cannot_attach_after_runner_start(tmp_path):
    f = LocalHost(tmp_path, port=0)
    try:
        with pytest.raises(ValueError): f.runner.attach_recovery_service(f.host)
        other = DaemonRunner(f.daemon, (), log_path=tmp_path / "other.log")
        other._start_entered = True
        with pytest.raises(ValueError): other.attach_recovery_service(f.host)
        async def lifecycle():
            await f.host.start()
            assert f.host.status == "serving"
            await f.host.stop()
            assert f.host.status == "stopped"
        asyncio.run(lifecycle())
        with pytest.raises(ValueError, match="already_used"): asyncio.run(f.host.start())
    finally: f.close()


@pytest.mark.parametrize("field,value", [("host", "0.0.0.0"), ("port", 8766), ("startup_timeout", 30.0)])
def test_transport_configuration_is_pinned_before_start(tmp_path, field, value):
    f = LocalHost(tmp_path, port=0)
    try:
        setattr(f.host, field, value)
        asyncio.run(f.host.start())
        assert f.host.status == "failed" and f.host._socket is None
        assert f.daemon.kill_switch.engaged
    finally: f.close()


def test_shutdown_before_start_does_not_later_activate_a_listener(tmp_path):
    f = LocalHost(tmp_path, port=0)
    try:
        asyncio.run(f.host.stop())
        assert f.host.status == "stopped"
        with pytest.raises(ValueError, match="already_used"): asyncio.run(f.host.start())
    finally: f.close()


def test_missing_credentials_at_start_holds_entry_without_creating_a_store(tmp_path):
    f = LocalHost(tmp_path, port=0)
    try:
        f.keys.close()
        path = f.keys.path
        before = path.read_bytes()
        asyncio.run(f.host.start())
        assert f.host.status == "failed" and f.host._socket is None
        assert f.daemon.kill_switch.engaged and path.read_bytes() == before
        assert f.operations._records() == []
    finally: f.close()


def test_failed_durable_halt_notification_still_keeps_the_entry_latch(tmp_path, monkeypatch):
    f = LocalHost(tmp_path, port=0)
    try:
        def broken(_): raise OSError("synthetic sensitive path must not be logged")
        monkeypatch.setattr(f.daemon, "engage_kill_switch", broken)
        f.keys.close()
        asyncio.run(f.host.start())
        assert f.daemon.kill_switch.engaged and f.host.failure_code == "recovery_listener_unavailable"
    finally: f.close()


def test_startup_deadline_retains_protection_and_closes_its_socket(tmp_path, monkeypatch):
    from quant_ai.operations.recovery_service import _BorrowedSignalServer
    f = LocalHost(tmp_path, port=0, startup_timeout=.03)
    async def never_ready(server, **_):
        while not server.should_exit: await asyncio.sleep(.01)
    monkeypatch.setattr(_BorrowedSignalServer, "serve", never_ready)
    async def scenario():
        task = asyncio.create_task(f.runner.start())
        try:
            await wait_for(lambda:f.host.status == "failed" and f.host._task.done())
            before = len(f.ticks); await asyncio.sleep(.03)
            assert len(f.ticks) > before and not task.done()
            assert f.host._socket.fileno() == -1 and f.daemon.kill_switch.engaged
        finally: await stop_runner(f, task)
    try: asyncio.run(scenario())
    finally: f.close()


def test_actual_institutional_daemon_and_loopback_recovery_share_account_and_protection(tmp_path, monkeypatch):
    from decimal import Decimal

    from test_institutional_daemon_cycle import NOW, CycleHarness

    from quant_ai.operations.recovery_service import RecoveryServiceHost
    monkeypatch.setenv("PRAMANA_RELEASE_REVISION", "a"*40)
    h = CycleHarness(tmp_path)
    keys = PersistentApiKeyRegistry(tmp_path / "keys.sqlite", create=True)
    operations = InstitutionalRecoveryOperations(h.runtime, tmp_path / "audit.sqlite")
    raw, _ = keys.issue("tenant", scopes=("institutional.recovery.read", "institutional.recovery.apply"),
                       expires_at=datetime.now(timezone.utc)+timedelta(hours=1))
    host = RecoveryServiceHost(daemon=h.daemon, keys=keys, operations=operations, port=0)
    h.runner.attach_recovery_service(host)
    h.runner.streams = ()  # No SDK/websocket connection; the real TickBuffer is seeded below.
    h.runner.clock = lambda: NOW
    h.runner.protection_interval = .02
    async def fast_clock(_): await asyncio.sleep(.03)
    h.runner.sleeper = fast_clock
    original = h.daemon.run_once
    async def scenario():
        cycle = asyncio.Event()
        async def one_cycle(now):
            result = await original(now)
            cycle.set()
            await asyncio.Future()  # Stop the next synthetic cadence, not the independent protection thread.
            return result
        monkeypatch.setattr(h.daemon, "run_once", one_cycle)
        h.seed()
        task = asyncio.create_task(h.runner.start())
        try:
            await wait_for(cycle.is_set)
            execution = h.daemon.scheduler.last_result.execution
            assert execution.fill is not None, execution.risk_decision.reason
            assert host.status == "serving" and len(h.broker.ledger_entries("tenant")) == 1
            pid = h.programs.db.execute("SELECT program_id FROM execution_programs").fetchone()[0]
            h.runtime.kill_switch.engage("retain this real-cycle synthetic halt")
            h.tick(Decimal(90))
            await wait_for(lambda: not h.broker.get_positions("tenant"))
            async with httpx.AsyncClient(timeout=5) as client:
                preview = await client.get(host.origin+f"/v1/institutional/programs/{pid}/recovery", headers={"X-API-Key":raw})
                assert preview.status_code == 200
                response = await client.post(host.origin+f"/v1/institutional/programs/{pid}/recovery", headers={"X-API-Key":raw},
                    json={"request_id":"actual-cycle-recovery", "expected_context_sha256":preview.json()["context_sha256"],
                          "confirmation":"reconcile_recorded_paper_bookkeeping_only"})
                assert response.status_code == 200, response.text
                assert response.json()["result"]["program_state"] == "COMPLETE"
            assert len(h.broker.ledger_entries("tenant")) == 2  # original BUY and independent protective SELL only
            assert h.broker.reconcile("tenant")["status"] == "matched"
            assert h.journal.native_balance("tenant", "SECURITIES_COST", "INR") == 0
            assert h.journal.native_balance("tenant", "CASH_AVAILABLE", "INR") == h.broker.get_margin("tenant").cash_balance
            assert h.runtime.kill_switch.engaged
        finally:
            h.runner.request_stop(); task.cancel(); await asyncio.gather(task, return_exceptions=True)
        assert host.status == "stopped" and keys.authenticate(raw) is not None
    try: asyncio.run(scenario())
    finally: operations.close(); keys.close(); h.close()



def test_stop_request_wakes_cadence_without_external_task_cancellation(tmp_path):
    f = LocalHost(tmp_path, port=0)
    async def scenario():
        task = asyncio.create_task(f.runner.start())
        await wait_for(lambda:f.host.status == "serving")
        await asyncio.sleep(.03)  # the runner is in its ordinary multi-minute cadence wait
        f.runner.request_stop()
        await asyncio.wait_for(task, 2)
        assert not task.cancelled() and f.host.status == "stopped"
        assert f.runner._protection_stop.is_set()
    try: asyncio.run(scenario())
    finally: f.close()


def test_stop_from_other_thread_wakes_the_selected_runner(tmp_path):
    f = LocalHost(tmp_path, port=0)
    async def scenario():
        task = asyncio.create_task(f.runner.start())
        await wait_for(lambda:f.host.status == "serving")
        await asyncio.sleep(.02)
        await asyncio.to_thread(f.runner.request_stop)
        await asyncio.wait_for(task, 2)
        assert f.host.status == "stopped"
    try: asyncio.run(scenario())
    finally: f.close()


def test_listener_preserves_process_signal_handlers_and_does_not_close_borrowed_stores(tmp_path):
    import signal
    f = LocalHost(tmp_path, port=0)
    async def scenario():
        # asyncio.run installs its own SIGINT handler before this coroutine starts.
        before = {sig:signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        await f.host.start()
        assert {sig:signal.getsignal(sig) for sig in before} == before
        assert f.host._server.config.proxy_headers is False
        assert f.host._server.config.workers == 1 and f.host._server.config.reload is False
        await asyncio.gather(f.host.stop(), f.host.stop())
        assert {sig:signal.getsignal(sig) for sig in before} == before
        assert f.keys.authenticate(f.raw) is not None
        assert f.operations._records() == []
        assert f.h.broker.get_positions("tenant") == ()
    try: asyncio.run(scenario())
    finally: f.close()


def test_paper_flag_change_before_service_start_refuses_without_socket(tmp_path, monkeypatch):
    f = LocalHost(tmp_path, port=0)
    try:
        monkeypatch.setenv("TRADING_LIVE_MONEY_ACTIVE", "true")
        asyncio.run(f.host.start())
        assert f.host.status == "failed" and f.host._socket is None
        assert f.daemon.kill_switch.engaged
    finally: f.close()
