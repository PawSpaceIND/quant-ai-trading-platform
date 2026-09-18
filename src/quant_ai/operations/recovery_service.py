"""Opt-in loopback recovery listener owned by the existing daemon supervisor.

The host borrows one selected runtime and already-open credential/audit stores. It
never builds another trading runtime, opens an account, grants a key or retries an
order. Shutdown drains admitted HTTP work before runner protection is stopped.
"""
from __future__ import annotations

import asyncio
import logging
import math
import socket
from contextlib import contextmanager

import uvicorn

from quant_ai.api.recovery_app import _paper_only, create_recovery_app
from quant_ai.operations.institutional_operator import InstitutionalRecoveryOperations
from quant_ai.security.persistent_api_keys import PersistentApiKeyRegistry

LOGGER = logging.getLogger(__name__)


class _BorrowedSignalServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        # The enclosing daemon/process owns signals; never replace its handlers.
        yield


class RecoveryServiceHost:
    """One-shot lifecycle for a single loopback listener and borrowed account.

    Supplying this object alone does not start a listener. Attach it to its exact
    DaemonRunner before start. An unexpected listener failure latches entry risk
    while the runner keeps its existing independent protection/cadence active.
    """

    def __init__(self, *, daemon, keys: PersistentApiKeyRegistry,
                 operations: InstitutionalRecoveryOperations, host: str = "127.0.0.1",
                 port: int = 8765, startup_timeout: float = 10.0):
        if (type(keys) is not PersistentApiKeyRegistry
                or type(operations) is not InstitutionalRecoveryOperations
                or type(host) is not str or host not in {"127.0.0.1", "::1"}
                or type(port) is not int or not 0 <= port <= 65535
                or type(startup_timeout) not in {int, float}
                or not math.isfinite(startup_timeout) or not 0 < startup_timeout <= 30
                or not _paper_only()):
            raise ValueError("recovery_listener_configuration_invalid")
        self.daemon, self.keys, self.operations = daemon, keys, operations
        self.host, self.port, self.startup_timeout = host, port, float(startup_timeout)
        self._selection = (daemon, keys, operations, operations.runtime)
        self._transport_selection = (host, port, float(startup_timeout))
        self._status = "not_started"
        self._origin = None
        self._server = None
        self._task = None
        self._stop_task = None
        self._socket = None
        self._stopping = False
        self._used = False
        self.failure_code = None
        self._assert_selection()

    @property
    def status(self):
        return self._status

    @property
    def origin(self):
        # An origin is an observation of this listener, never account readiness.
        return self._origin if self._status == "serving" else None

    def _assert_selection(self):
        try:
            expected = self.daemon.scheduler.pipeline.runtime
            actual = (self.daemon, self.keys, self.operations, self.operations.runtime)
            valid = (all(a is b for a, b in zip(actual, self._selection))
                     and expected is self.operations.runtime
                     and self.daemon.tenant_id == self.operations.tenant_id
                     and self.operations.runtime.kill_switch is self.daemon.kill_switch
                     and callable(self.daemon.engage_kill_switch)
                     and type(self.host) is str and type(self.port) is int
                     and type(self.startup_timeout) is float
                     and (self.host, self.port, self.startup_timeout) == self._transport_selection)
        except (AttributeError, TypeError):
            valid = False
        if not valid:
            raise ValueError("recovery_listener_runtime_mismatch")

    def _fail(self):
        self.failure_code = "recovery_listener_unavailable"
        self._status = "failed"
        # Never clear or replace an existing halt. Memory is latched before trying
        # durable notification, so a logging/storage fault cannot imply permission.
        if not self.daemon.kill_switch.engaged:
            self.daemon.kill_switch.engage(self.failure_code)
            try:
                self.daemon.engage_kill_switch(self.failure_code)
            except Exception:  # noqa: BLE001 - final halt persistence boundary must not stop protection
                self.daemon.kill_switch.engage(self.failure_code)
                LOGGER.error("recovery_listener_halt_persistence_failed")
        LOGGER.error("recovery_listener_unavailable")

    async def _serve(self):
        finished = False
        try:
            await self._server.serve(sockets=[self._socket])
            finished = True
        except asyncio.CancelledError:
            if not self._stopping:
                self._fail()
            raise
        except (Exception, SystemExit):  # noqa: BLE001 - contain server failure, preserve daemon protection
            # Transport diagnostics may include host configuration. Fixed code only.
            self._fail()
        finally:
            if not self._stopping and self.failure_code is None:
                self._fail()
            if not finished and hasattr(self._server, "servers"):
                # A cancelled/crashed listener must still drain work already admitted.
                await self._server.shutdown(sockets=[self._socket])

    async def start(self):
        if self._used or self._stopping:
            raise ValueError("recovery_listener_already_used")
        self._used = True
        self._status = "starting"
        try:
            self._assert_selection()
            if not _paper_only():
                raise ValueError("recovery_listener_paper_only")
            self.keys._check_file()
            app = create_recovery_app(keys=self.keys,
                operations={self.operations.tenant_id: self.operations})
            family = socket.AF_INET6 if self.host == "::1" else socket.AF_INET
            self._socket = socket.socket(family, socket.SOCK_STREAM)
            self._socket.setblocking(False)
            self._socket.bind((self.host, self.port))
            self._socket.listen(32)
            selected_port = self._socket.getsockname()[1]
            self._origin = f"http://[{self.host}]:{selected_port}" if family == socket.AF_INET6 else f"http://{self.host}:{selected_port}"
            self._server = _BorrowedSignalServer(uvicorn.Config(
                app, host=self.host, port=selected_port, loop="asyncio", http="h11", ws="none",
                access_log=False, log_config=None, log_level="error", proxy_headers=False,
                server_header=False, workers=1, reload=False, limit_concurrency=16,
                backlog=32, timeout_keep_alive=1, timeout_graceful_shutdown=None,
                h11_max_incomplete_event_size=8192))
            self._task = asyncio.create_task(self._serve(), name="pramana-recovery-listener")
            async def ready():
                while not self._server.started and not self._task.done():
                    await asyncio.sleep(.01)
            await asyncio.wait_for(ready(), timeout=self.startup_timeout)
            if self._task.done() or not self._server.started:
                raise ValueError("recovery_listener_not_started")
            self._status = "serving"
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - listener startup cannot take independent protection down
            self._fail()
            await self.stop()

    async def stop(self):
        """Stop accepting, then drain before returning or propagating cancellation.

        No forced timeout cancels admitted bookkeeping. A stuck operation requires
        operator intervention; the enclosing runner retains protection meanwhile.
        Stores remain caller-owned. Concurrent stop callers await the same drain.
        """
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(self._drain(), name="pramana-recovery-drain")
        cancelled = False
        while not self._stop_task.done():
            try:
                await asyncio.shield(self._stop_task)
            except asyncio.CancelledError:
                cancelled = True
        self._stop_task.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _drain(self):
        self._stopping = True
        if self.failure_code is None:
            self._status = "draining"
        if self._server is not None:
            self._server.should_exit = True
            for listener in getattr(self._server, "servers", ()):
                listener.close()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
        if self._server is not None:
            for listener in getattr(self._server, "servers", ()):
                listener.close()
        if self._socket is not None:
            self._socket.close()
        self._origin = None
        self._status = "failed" if self.failure_code is not None else "stopped"
