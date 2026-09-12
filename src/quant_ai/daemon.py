from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from importlib import import_module
from pathlib import Path
from typing import Any

from quant_ai.agents.swarm import AtlasCIOAgent
from quant_ai.agents.swarm_runtime import SwarmPaperTradingService
from quant_ai.domain.models import AssetClass, Instrument, Market, RiskMode
from quant_ai.execution.audit import PRAMANA_PROOF_DIRECTORY, XAITraceLogger
from quant_ai.execution.daemon import AutonomousTradingDaemon
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.execution.portfolio import PortfolioTracker
from quant_ai.execution.scheduler import AutonomousCadenceScheduler
from quant_ai.intelligence.pipeline import SwarmMarketAnalysisPipeline
from quant_ai.intelligence.sandbox import (
    SandboxFundamentalDataProvider,
    SandboxMacroIndicatorProvider,
    SandboxNewsSentimentProvider,
)
from quant_ai.marketdata.feed import UsaSandboxMarketDataFeed
from quant_ai.marketdata.ticker_stream import (
    AbstractTickerStream,
    IBKRAsyncTicker,
    TickBuffer,
    ZerodhaKiteTicker,
)
from quant_ai.planning.capital import CapitalGoalEngine, CapitalPlanRequest

Clock = Callable[[], datetime]
Sleeper = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class ReconnectPolicy:
    initial_delay_seconds: float = 1.0
    max_delay_seconds: float = 60.0
    multiplier: float = 2.0

    def __post_init__(self) -> None:
        if self.initial_delay_seconds <= 0 or self.max_delay_seconds <= 0:
            raise ValueError("reconnect delays must be positive")
        if self.max_delay_seconds < self.initial_delay_seconds:
            raise ValueError("max reconnect delay cannot be below initial delay")
        if self.multiplier < 1:
            raise ValueError("reconnect multiplier must be at least one")


class DaemonRunner:
    """24/7 ghost-mode supervisor for websocket feeds and the 10-minute swarm cadence."""

    def __init__(
        self,
        daemon: AutonomousTradingDaemon,
        streams: Iterable[AbstractTickerStream],
        *,
        cadence: timedelta = timedelta(minutes=10),
        reconnect: ReconnectPolicy | None = None,
        log_path: str | Path = "pramana-ghost.log",
        clock: Clock | None = None,
        sleeper: Sleeper = asyncio.sleep,
    ) -> None:
        _assert_ghost_mode()
        if cadence <= timedelta(0):
            raise ValueError("cadence must be positive")
        self.daemon = daemon
        self.streams = tuple(streams)
        self.cadence = cadence
        self.reconnect = reconnect or ReconnectPolicy()
        self.log_path = Path(log_path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sleeper = sleeper
        self._stop_requested = False
        self._proof_count = 0

    def request_stop(self) -> None:
        self._stop_requested = True
        self.daemon.request_stop()

    async def start(self) -> None:
        supervisors = [asyncio.create_task(self._supervise_stream(stream)) for stream in self.streams]
        cadence_task = asyncio.create_task(self._run_aligned_cadence())
        try:
            await cadence_task
        finally:
            self._stop_requested = True
            for task in supervisors:
                task.cancel()
            await asyncio.gather(*supervisors, return_exceptions=True)
            await self._stop_streams()

    async def _supervise_stream(self, stream: AbstractTickerStream) -> None:
        delay = self.reconnect.initial_delay_seconds
        while not self._stop_requested:
            try:
                await stream.start()
                delay = self.reconnect.initial_delay_seconds
                error = await stream.wait_for_connection_error()
                if self._stop_requested:
                    return
                await self._write_event("websocket_disconnected", stream=type(stream).__name__, error=str(error))
            except asyncio.CancelledError:
                raise
            except (ConnectionError, OSError, TimeoutError) as error:
                await self._write_event("websocket_connect_failed", stream=type(stream).__name__, error=str(error))
            finally:
                await _safe_stop(stream)
            if not self._stop_requested:
                await self.sleeper(delay)
                delay = min(self.reconnect.max_delay_seconds, delay * self.reconnect.multiplier)

    async def _run_aligned_cadence(self) -> None:
        while not self._stop_requested:
            now = _as_utc(self.clock())
            boundary = _next_boundary(now, self.cadence)
            await self.sleeper(max(0.0, (boundary - now).total_seconds()))
            if self._stop_requested:
                return
            current = _as_utc(self.clock())
            await self.daemon.run_once(current)
            await self._capture_xai_proofs(current)

    async def _capture_xai_proofs(self, generated_at: datetime) -> None:
        logger = self.daemon.scheduler.pipeline.runtime.xai_logger
        traces = logger.traces()
        for trace in traces[self._proof_count :]:
            payload = {
                "event": "xai_proof",
                "generated_at": generated_at.isoformat(),
                "proof": json.loads(logger.to_json(trace)),
            }
            await asyncio.to_thread(self._append_json_line, payload)
        self._proof_count = len(traces)

    async def _write_event(self, event: str, **fields: str) -> None:
        payload = {"event": event, "generated_at": _as_utc(self.clock()).isoformat(), **fields}
        await asyncio.to_thread(self._append_json_line, payload)

    def _append_json_line(self, payload: dict[str, Any]) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")

    async def _stop_streams(self) -> None:
        await asyncio.gather(*(_safe_stop(stream) for stream in self.streams), return_exceptions=True)
        self.daemon.tracker.broker.flush()



def build_ghost_runner(
    *,
    zerodha_api_key: str,
    zerodha_access_token: str,
    zerodha_instrument_tokens: Iterable[int],
    zerodha_symbol_by_token: dict[int, str],
    ib_client: Any,
    ib_contracts: Iterable[Any],
    ib_host: str | None = None,
    ib_port: int = 7497,
    ib_client_id: int = 17,
    database: str | Path = "quant-ai-paper.db",
    tenant_id: str = "ghost",
    log_path: str | Path = "pramana-ghost.log",
    xai_directory: str | Path = PRAMANA_PROOF_DIRECTORY,
) -> DaemonRunner:
    """Assemble the ghost runtime with live market data and paper-only execution."""
    _assert_ghost_mode()
    broker = PaperBrokerService(database, starting_capital=Decimal(100000))
    feed = UsaSandboxMarketDataFeed()
    cio = AtlasCIOAgent()
    runtime = SwarmPaperTradingService(
        cio=cio,
        broker=broker,
        xai_logger=XAITraceLogger(xai_directory),
    )
    pipeline = SwarmMarketAnalysisPipeline(
        feed,
        SandboxNewsSentimentProvider(),
        SandboxFundamentalDataProvider(),
        SandboxMacroIndicatorProvider(),
        runtime=runtime,
    )
    scheduler = AutonomousCadenceScheduler(pipeline, cadence=timedelta(minutes=10))
    tracker = PortfolioTracker(broker, feed, tenant_id=tenant_id)
    plan = CapitalGoalEngine().recommend(
        CapitalPlanRequest(
            Decimal(100000),
            Decimal("0.80"),
            Decimal("0.20"),
            expected_edge=Decimal("0.02"),
            requested_mode=RiskMode.BALANCED,
        )
    )
    instrument = Instrument("AAPL", Market.USA, AssetClass.EQUITY, "USD", "NASDAQ")
    daemon = AutonomousTradingDaemon(
        scheduler,
        tracker,
        instrument,
        plan,
        quantity=10,
        country="USA",
        tenant_id=tenant_id,
    )
    buffer = TickBuffer()
    streams = (
        ZerodhaKiteTicker(
            zerodha_api_key,
            zerodha_access_token,
            zerodha_instrument_tokens,
            zerodha_symbol_by_token,
            buffer,
        ),
        IBKRAsyncTicker(
            ib_client,
            ib_contracts,
            buffer,
            connect_host=ib_host,
            connect_port=ib_port,
            client_id=ib_client_id,
        ),
    )
    return DaemonRunner(daemon, streams, cadence=timedelta(minutes=10), log_path=log_path)

def _assert_ghost_mode() -> None:
    if os.getenv("TRADING_LIVE_MONEY_ACTIVE", "false").strip().lower() == "true":
        raise RuntimeError("ghost daemon refuses to start when TRADING_LIVE_MONEY_ACTIVE=true")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _next_boundary(now: datetime, cadence: timedelta) -> datetime:
    seconds = cadence.total_seconds()
    if seconds <= 0:
        raise ValueError("cadence must be positive")
    epoch = now.timestamp()
    next_epoch = (int(epoch // seconds) + 1) * seconds
    return datetime.fromtimestamp(next_epoch, tz=timezone.utc)


async def _safe_stop(stream: AbstractTickerStream) -> None:
    try:
        await stream.stop()
    except (ConnectionError, OSError, TimeoutError):
        return


def _env_json(name: str, default: Any) -> Any:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return json.loads(raw)


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


def build_ghost_runner_from_env() -> DaemonRunner:
    """Build the headless ghost runner from deployment environment variables."""
    _assert_ghost_mode()
    ib_module = import_module("ib_async")
    ib = ib_module.IB()
    contracts = tuple(
        ib_module.Contract(**item)
        for item in _env_json("PRAMANA_IB_CONTRACTS_JSON", [])
    )
    tokens = tuple(int(item) for item in _env_json("PRAMANA_ZERODHA_TOKENS_JSON", []))
    raw_symbols = _env_json("PRAMANA_ZERODHA_SYMBOLS_JSON", {})
    symbols = {int(key): str(value) for key, value in raw_symbols.items()}
    return build_ghost_runner(
        zerodha_api_key=_required_env("ZERODHA_API_KEY"),
        zerodha_access_token=_required_env("ZERODHA_ACCESS_TOKEN"),
        zerodha_instrument_tokens=tokens,
        zerodha_symbol_by_token=symbols,
        ib_client=ib,
        ib_contracts=contracts,
        ib_host=os.getenv("PRAMANA_IB_HOST", "127.0.0.1"),
        ib_port=int(os.getenv("PRAMANA_IB_PORT", "7497")),
        ib_client_id=int(os.getenv("PRAMANA_IB_CLIENT_ID", "17")),
        database=os.getenv("PRAMANA_PAPER_DB", "/var/lib/pramana/pramana.db"),
        tenant_id=os.getenv("PRAMANA_TENANT_ID", "ghost"),
        log_path=os.getenv("PRAMANA_GHOST_LOG", "/var/log/pramana/pramana-ghost.log"),
        xai_directory=os.getenv("PRAMANA_XAI_DIR", "/var/lib/pramana/xai"),
    )


def main() -> int:
    runner = build_ghost_runner_from_env()
    asyncio.run(runner.start())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
