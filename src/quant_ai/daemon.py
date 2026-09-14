from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from importlib import import_module
from pathlib import Path
from threading import Event, Thread
from typing import Any

from quant_ai.agents.atlas import AtlasInvestmentAgent
from quant_ai.agents.swarm import AtlasCIOAgent
from quant_ai.agents.swarm_runtime import SwarmPaperTradingService
from quant_ai.config import paths
from quant_ai.domain.models import AssetClass, Instrument, Market
from quant_ai.execution.audit import PRAMANA_PROOF_DIRECTORY, XAITraceLogger
from quant_ai.execution.daemon import AutonomousTradingDaemon
from quant_ai.execution.notifications import (
    ConsoleNotificationAdapter,
    TelegramNotificationAdapter,
    TradingNotificationDispatcher,
)
from quant_ai.execution.paper_ledger import PaperBrokerService
from quant_ai.execution.portfolio import PortfolioTracker
from quant_ai.execution.scheduler import AutonomousCadenceScheduler
from quant_ai.execution.session import (
    GlobalVenue,
    MarketCalendar,
    default_holidays,
    holidays_from_json,
)
from quant_ai.governance.directives import FounderDirectives, country_for
from quant_ai.intelligence.external.fred import FredMacroProvider
from quant_ai.intelligence.external.rss import RssNewsSentimentAdapter
from quant_ai.intelligence.pipeline import SwarmMarketAnalysisPipeline
from quant_ai.intelligence.providers import (
    FundamentalDataProvider,
    MacroIndicatorProvider,
    NewsSentimentProvider,
)
from quant_ai.intelligence.resilience import ResilientHttpClient, UrllibTransport
from quant_ai.intelligence.sandbox import (
    SandboxFundamentalDataProvider,
    SandboxMacroIndicatorProvider,
    SandboxNewsSentimentProvider,
)
from quant_ai.llm.anthropic_client import AnthropicSwarmClient
from quant_ai.marketdata.live_feed import LiveTickMarketDataFeed
from quant_ai.marketdata.ticker_stream import (
    AbstractTickerStream,
    IBKRAsyncTicker,
    TickBuffer,
    ZerodhaKiteTicker,
    _contract_symbol,
)
from quant_ai.orchestration.cadence import CadenceMarketReader
from quant_ai.planning.capital import CapitalGoalEngine
from quant_ai.risk.warden import RiskWarden

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


@dataclass(frozen=True)
class CadenceFaultPolicy:
    """C3: a failing cadence tick backs off instead of taking the process down.

    Escalating to the kill switch matters as much as surviving: a daemon that crash-loops
    under systemd `Restart=on-failure` looks healthy while repeatedly resetting its own
    circuit breakers. Persistent failure must become a visible, latched halt.
    """

    backoff_seconds: tuple[float, ...] = (5.0, 15.0, 60.0)
    halt_after_consecutive_failures: int = 5

    def __post_init__(self) -> None:
        if not self.backoff_seconds or any(item < 0 for item in self.backoff_seconds):
            raise ValueError("backoff_seconds must be a non-empty series of non-negative delays")
        if self.halt_after_consecutive_failures < 1:
            raise ValueError("halt_after_consecutive_failures must be at least one")

    def delay_for(self, consecutive_failures: int) -> float:
        index = min(max(consecutive_failures, 1), len(self.backoff_seconds)) - 1
        return self.backoff_seconds[index]


class DaemonRunner:
    """24/7 ghost-mode supervisor for websocket feeds and the 10-minute swarm cadence."""

    def __init__(
        self,
        daemon: AutonomousTradingDaemon,
        streams: Iterable[AbstractTickerStream],
        *,
        cadence: timedelta = timedelta(minutes=10),
        reconnect: ReconnectPolicy | None = None,
        fault_policy: CadenceFaultPolicy | None = None,
        log_path: str | Path = "pramana-ghost.log",
        clock: Clock | None = None,
        sleeper: Sleeper = asyncio.sleep,
        protection_interval: float = 1.0,
    ) -> None:
        _assert_ghost_mode()
        if cadence <= timedelta(0):
            raise ValueError("cadence must be positive")
        if protection_interval <= 0:
            raise ValueError("protection interval must be positive")
        self.protection_interval = protection_interval
        self._protection_stop = Event()
        self.daemon = daemon
        self.streams = tuple(streams)
        self.cadence = cadence
        self.reconnect = reconnect or ReconnectPolicy()
        self.fault_policy = fault_policy or CadenceFaultPolicy()
        self.log_path = Path(log_path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sleeper = sleeper
        self._stop_requested = False
        self._proof_count = 0
        self._consecutive_failures = 0
        self._logger = logging.getLogger("quant_ai.ghost_runner")

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def request_stop(self) -> None:
        self._stop_requested = True
        self._protection_stop.set()
        self.daemon.request_stop()

    def _protect(self) -> None:
        # A dedicated thread keeps protection responsive even when synchronous provider
        # I/O blocks the analysis event loop. Ledger operations share one RLock.
        while not self._protection_stop.is_set():
            try:
                self.daemon.protection_tick(self.clock())
            except Exception as error:
                self._logger.exception("protection_tick_failed")
                try:
                    self.daemon.engage_kill_switch(f"protection_failure:{type(error).__name__}")
                except Exception:
                    self._logger.exception("protection_halt_persistence_failed")
            self._protection_stop.wait(self.protection_interval)

    async def start(self) -> None:
        protection = Thread(target=self._protect, name="pramana-protection", daemon=True)
        protection.start()
        supervisors = [asyncio.create_task(self._supervise_stream(stream)) for stream in self.streams]
        cadence_task = asyncio.create_task(self._run_aligned_cadence())
        try:
            await cadence_task
        finally:
            self._stop_requested = True
            self._protection_stop.set()
            protection.join(timeout=10)
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
            try:
                await self.daemon.run_once(current)
                await self._capture_xai_proofs(current)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - the supervisor is the last line
                await self._handle_cadence_failure(error, current)
            else:
                if self._consecutive_failures:
                    self._logger.info(
                        "cadence_recovered after %d consecutive failures",
                        self._consecutive_failures,
                    )
                    await self._write_event(
                        "cadence_recovered",
                        consecutive_failures=str(self._consecutive_failures),
                    )
                self._consecutive_failures = 0

    async def _handle_cadence_failure(self, error: BaseException, at: datetime) -> None:
        """Absorb a failing tick: log it, back off, and latch a halt if it keeps failing."""
        self._consecutive_failures += 1
        detail = f"{type(error).__name__}: {error}"
        self._logger.exception(
            "cadence_tick_failed consecutive=%d detail=%s",
            self._consecutive_failures,
            detail,
        )
        await self._write_event(
            "cadence_tick_failed",
            consecutive_failures=str(self._consecutive_failures),
            error=detail,
            occurred_at=at.isoformat(),
        )
        self.daemon.notify_cadence_failure(detail, self._consecutive_failures)
        if self._consecutive_failures >= self.fault_policy.halt_after_consecutive_failures:
            reason = (
                f"cadence failed {self._consecutive_failures} times consecutively: {detail}"
            )
            self.daemon.engage_kill_switch(reason)
            self._logger.critical("cadence_halted reason=%s", reason)
            await self._write_event("cadence_halted", reason=reason)
        await self.sleeper(self.fault_policy.delay_for(self._consecutive_failures))

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
    llm_client: AnthropicSwarmClient | None = None,
    instrument: Instrument | None = None,
    include_ibkr: bool = True,
    news_provider: NewsSentimentProvider | None = None,
    fundamentals_provider: FundamentalDataProvider | None = None,
    macro_provider: MacroIndicatorProvider | None = None,
    holidays: dict[Market | GlobalVenue, frozenset[date]] | None = None,
    notifications: TradingNotificationDispatcher | None = None,
    halt_file: str | Path | None = None,
    directives: FounderDirectives | None = None,
    pilot_mode: bool = False,
) -> DaemonRunner:
    """Assemble the ghost runtime with live market data and paper-only execution."""
    _assert_ghost_mode()
    directives = directives or FounderDirectives()
    broker = PaperBrokerService(database, starting_capital=directives.starting_capital)
    buffer = TickBuffer()
    # Candles and marks come from the websocket ticks themselves, for any market the
    # streams can subscribe to. Nothing in the live runtime touches a synthetic price.
    feed = LiveTickMarketDataFeed(buffer)
    cio = AtlasCIOAgent(
        AtlasInvestmentAgent(llm_client=llm_client, founder_instructions=directives.instructions)
    )
    runtime = SwarmPaperTradingService(
        cio=cio,
        warden=RiskWarden(blocked_asset_classes=directives.blocked_asset_classes()),
        broker=broker,
        xai_logger=XAITraceLogger(xai_directory),
        max_open_positions=directives.max_open_positions,
    )
    pipeline = SwarmMarketAnalysisPipeline(
        feed,
        news_provider or SandboxNewsSentimentProvider(),
        fundamentals_provider or SandboxFundamentalDataProvider(),
        macro_provider or SandboxMacroIndicatorProvider(),
        runtime=runtime,
        tick_reader=CadenceMarketReader(buffer),
    )
    scheduler = AutonomousCadenceScheduler(
        pipeline,
        cadence=timedelta(minutes=10),
        calendar=MarketCalendar(holidays=holidays if holidays is not None else default_holidays()),
    )
    tracker = PortfolioTracker(broker, feed, tenant_id=tenant_id)
    plan = CapitalGoalEngine().recommend(directives.capital_plan_request())
    instrument = instrument or Instrument("AAPL", Market.USA, AssetClass.EQUITY, "USD", "NASDAQ")
    instruments = directives.instruments_or(instrument)
    if pilot_mode:
        broker.configure_pilot(instruments, tenant_id)
    mapped = set(zerodha_symbol_by_token.values())
    if include_ibkr:
        mapped.update(_contract_symbol(contract) for contract in ib_contracts)
    for item in instruments:
        if item.symbol not in mapped:
            logging.getLogger("quant_ai.ghost_runner").warning(
                "watchlist symbol %s has no websocket mapping; it will be vetoed as missing market data",
                item.symbol,
            )
    daemon = AutonomousTradingDaemon(
        scheduler,
        tracker,
        instruments[0],
        plan,
        # quantity is intentionally unset: sized per tick from live equity and the plan.
        country=country_for(instruments[0]),
        tenant_id=tenant_id,
        notifications=notifications,
        halt_file=halt_file,
        instruments=instruments,
    )
    if pilot_mode:
        daemon.enable_pilot_monitoring()
    streams: list[AbstractTickerStream] = [
        ZerodhaKiteTicker(
            zerodha_api_key,
            zerodha_access_token,
            zerodha_instrument_tokens,
            zerodha_symbol_by_token,
            buffer,
        )
    ]
    if include_ibkr:
        streams.append(
            IBKRAsyncTicker(
                ib_client,
                ib_contracts,
                buffer,
                connect_host=ib_host,
                connect_port=ib_port,
                client_id=ib_client_id,
            )
        )
    if pilot_mode:
        daemon.bind_strategy_manifest(streams)
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


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


def _env_intelligence_providers() -> tuple[
    NewsSentimentProvider, FundamentalDataProvider, MacroIndicatorProvider
]:
    """Production providers return missing data when unavailable, never sandbox opinions."""
    from quant_ai.intelligence.failover import (
        FailoverFundamentalProvider,
        FailoverMacroProvider,
        FailoverNewsProvider,
        ProviderCategory,
        ProviderFailoverRegistry,
    )

    registry = ProviderFailoverRegistry()
    client = ResilientHttpClient(UrllibTransport())
    feeds = tuple(item.strip() for item in os.getenv("PRAMANA_NEWS_RSS_URLS", "").split(",") if item.strip())
    if feeds:
        registry.register(ProviderCategory.NEWS, RssNewsSentimentAdapter(client, feeds))
    fred_key = os.getenv("FRED_API_KEY", "").strip()
    if fred_key:
        registry.register(ProviderCategory.MACRO, FredMacroProvider(client, fred_key))
    return (
        FailoverNewsProvider(registry),
        FailoverFundamentalProvider(registry),
        FailoverMacroProvider(registry),
    )


def _env_holidays() -> dict[Market | GlobalVenue, frozenset[date]]:
    payload = _env_json("PRAMANA_HOLIDAYS_JSON", {})
    return holidays_from_json(payload, default_holidays()) if payload else default_holidays()


def _env_notifications() -> TradingNotificationDispatcher | None:
    token = os.getenv("PRAMANA_TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("PRAMANA_TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return None
    return TradingNotificationDispatcher(
        (ConsoleNotificationAdapter(), TelegramNotificationAdapter(token, chat_id))
    )


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
    instrument = Instrument(
        os.getenv("PRAMANA_TARGET_SYMBOL", "INFY").strip() or "INFY",
        Market(os.getenv("PRAMANA_TARGET_MARKET", "INDIA").strip().upper()),
        AssetClass(os.getenv("PRAMANA_TARGET_ASSET_CLASS", "EQUITY").strip().upper()),
        os.getenv("PRAMANA_TARGET_CURRENCY", "INR").strip().upper(),
        os.getenv("PRAMANA_TARGET_EXCHANGE", "NSE").strip().upper(),
    )
    news, fundamentals, macro = _env_intelligence_providers()
    return build_ghost_runner(
        directives=FounderDirectives.from_env(),
        pilot_mode=_env_flag("PRAMANA_PILOT_MODE", True),
        news_provider=news,
        fundamentals_provider=fundamentals,
        macro_provider=macro,
        holidays=_env_holidays(),
        notifications=_env_notifications(),
        halt_file=paths.halt_file(),
        zerodha_api_key=_required_env("ZERODHA_API_KEY"),
        zerodha_access_token=_required_env("ZERODHA_ACCESS_TOKEN"),
        zerodha_instrument_tokens=tokens,
        zerodha_symbol_by_token=symbols,
        ib_client=ib,
        ib_contracts=contracts,
        ib_host=os.getenv("PRAMANA_IB_HOST", "127.0.0.1"),
        ib_port=int(os.getenv("PRAMANA_IB_PORT", "7497")),
        ib_client_id=int(os.getenv("PRAMANA_IB_CLIENT_ID", "17")),
        database=str(paths.ledger_path("PRAMANA_PAPER_DB")),
        tenant_id=paths.tenant_id(default="ghost"),
        log_path=os.getenv("PRAMANA_GHOST_LOG", "/var/log/pramana/pramana-ghost.log"),
        xai_directory=str(paths.proof_directory("PRAMANA_XAI_DIR")),
        llm_client=AnthropicSwarmClient(),
        instrument=instrument,
        include_ibkr=_env_flag("PRAMANA_IBKR_ENABLED"),
    )


def main() -> int:
    runner = build_ghost_runner_from_env()
    asyncio.run(runner.start())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
