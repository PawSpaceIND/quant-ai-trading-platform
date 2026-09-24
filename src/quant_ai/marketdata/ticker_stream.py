from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections import Counter, deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from importlib import import_module
from threading import RLock
from typing import Any, Callable

from quant_ai.marketdata.tick_integrity import tick_value_issue, utc_time


@dataclass(frozen=True)
class LiveTick:
    symbol: str
    ltp: Decimal
    volume: Decimal
    bid: Decimal | None
    ask: Decimal | None
    observed_at: datetime
    source: str

    @property
    def spread(self) -> Decimal | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid


@dataclass(frozen=True)
class OrderBookUpdate:
    symbol: str
    bids: tuple[tuple[Decimal, Decimal], ...]
    asks: tuple[tuple[Decimal, Decimal], ...]
    observed_at: datetime
    source: str


class TickBuffer:
    def __init__(self, maxlen: int = 10_000, *, clock: Callable[[], datetime] | None = None) -> None:
        if maxlen < 1:
            raise ValueError("maxlen must be positive")
        self._ticks: deque[LiveTick] = deque(maxlen=maxlen)
        self._latest: dict[str, LiveTick] = {}
        self._listeners: list[Callable[[LiveTick], None]] = []
        self._lock = RLock()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._accepted = 0
        self._rejected: Counter[str] = Counter()
        self._last_rejection: dict | None = None

    def subscribe(self, listener: Callable[[LiveTick], None]) -> None:
        """Register a synchronous callback for accepted ticks, in acceptance order."""
        with self._lock:
            self._listeners.append(listener)

    def reject(self, reason: str, symbol: str, observed_at: datetime | None = None) -> None:
        with self._lock:
            self._rejected[reason] += 1
            self._last_rejection = {"reason": reason, "symbol": str(symbol)[:80],
                "observedAt": utc_time(observed_at).isoformat() if isinstance(observed_at, datetime) else None,
                "receivedAt": utc_time(self.clock()).isoformat()}

    def integrity(self) -> dict:
        with self._lock:
            return {"schema": "pramana.tick_integrity.v1", "accepted": self._accepted,
                "rejected": dict(self._rejected), "lastRejection": dict(self._last_rejection) if self._last_rejection else None,
                "scope": "current_process; rejected_ticks_do_not_refresh_quotes; no_exchange_sequence_reconstruction"}

    def put(self, tick: LiveTick) -> bool:
        with self._lock:
            issue = tick_value_issue(tick)
            try:
                observed = utc_time(tick.observed_at)
            except (TypeError, ValueError):
                self.reject("invalid_tick_timestamp", tick.symbol)
                return False
            if issue or observed > utc_time(self.clock()):
                self.reject(issue or "future_tick", tick.symbol, observed)
                return False
            previous = self._latest.get(tick.symbol)
            if previous and observed < utc_time(previous.observed_at):
                self.reject("out_of_order_tick", tick.symbol, observed)
                return False
            if previous == tick:
                self.reject("duplicate_tick", tick.symbol, observed)
                return False
            self._ticks.append(tick)
            self._latest[tick.symbol] = tick
            self._accepted += 1
            # Keep callbacks ordered across producer threads. Callbacks must be
            # short and must not wait for another thread to acquire this buffer.
            for listener in tuple(self._listeners):
                listener(tick)
            return True

    def latest(self, symbol: str) -> LiveTick | None:
        with self._lock:
            return self._latest.get(symbol)

    def latest_at_or_before(self, symbol: str, cutoff: datetime) -> LiveTick | None:
        """Newest retained accepted tick at the caller's event-time cutoff.

        A concurrent stream update must not move an older analysis into the future.
        The existing bounded deque is the only history; eviction returns None rather
        than widening the cutoff, changing timestamps or fetching historical prices.
        This is not proof of receipt-time availability at the cutoff.
        """
        current = utc_time(cutoff)
        with self._lock:
            latest = self._latest.get(symbol)
            if latest is None or utc_time(latest.observed_at) <= current:
                return latest
            for tick in reversed(self._ticks):
                if tick.symbol == symbol and utc_time(tick.observed_at) <= current:
                    return tick
            return None

    def snapshot(self) -> dict[str, LiveTick]:
        with self._lock:
            return dict(self._latest)


class TickerGaveUp(ConnectionError):
    """The stream's own reconnection is exhausted; only a fresh connection can recover it."""


class AbstractTickerStream(ABC):
    def __init__(self, buffer: TickBuffer | None = None) -> None:
        self.buffer = buffer or TickBuffer()
        self._connection_errors: asyncio.Queue[Exception] | None = None

    async def on_tick(self, tick: LiveTick) -> None:
        self.buffer.put(tick)

    async def on_orderbook_update(self, update: OrderBookUpdate) -> None:
        _ = update

    def recovers_in_place(self, error: Exception) -> bool:
        """Whether the stream reconnects by itself after ``error``.

        False by default: the supervisor stops the stream and starts it again. A stream
        whose client library already retries must say True for a routine drop, because
        stopping it cancels that retry.
        """
        _ = error
        return False

    async def on_connection_error(self, error: Exception) -> None:
        queue = self._error_queue()
        if queue.full():
            queued = queue.get_nowait()
            # A drop the stream recovers from is only journaled; a give-up is what makes
            # the supervisor reconnect, so a later routine drop must never displace it.
            if self.recovers_in_place(error) and not self.recovers_in_place(queued):
                error = queued
        queue.put_nowait(error)

    async def wait_for_connection_error(self) -> Exception:
        return await self._error_queue().get()

    def _error_queue(self) -> asyncio.Queue[Exception]:
        if self._connection_errors is None:
            self._connection_errors = asyncio.Queue(maxsize=1)
        return self._connection_errors

    @abstractmethod
    async def start(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def stop(self) -> None:
        raise NotImplementedError


class ZerodhaKiteTicker(AbstractTickerStream):
    def __init__(
        self,
        api_key: str,
        access_token: str,
        instrument_tokens: Iterable[int],
        symbol_by_token: dict[int, str],
        buffer: TickBuffer | None = None,
    ) -> None:
        super().__init__(buffer)
        self.api_key = api_key
        self.access_token = access_token
        self.instrument_tokens = tuple(instrument_tokens)
        self.symbol_by_token = dict(symbol_by_token)
        self._ticker: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        module = import_module("kiteconnect")
        ticker_type = module.KiteTicker
        self._loop = asyncio.get_running_loop()
        self._ticker = ticker_type(self.api_key, self.access_token)
        self._ticker.on_connect = self._on_connect
        self._ticker.on_ticks = self._on_ticks
        self._ticker.on_error = self._on_error
        self._ticker.on_close = self._on_close
        self._ticker.on_noreconnect = self._on_noreconnect
        reactor = _kite_reactor()
        if reactor is not None and reactor.running:
            # KiteTicker runs one Twisted reactor per process and starts it on the first
            # connect only. A later connect from this thread registers a socket with a
            # reactor that is not thread-safe and never wakes for it: on 24 September 2026
            # the replacement connection never opened, never failed, and the feed stayed
            # silent from 07:06 IST until the unpriced-stop halt at 09:17.
            reactor.callFromThread(self._ticker.connect, threaded=True)
        else:
            await asyncio.to_thread(self._ticker.connect, threaded=True)

    async def stop(self) -> None:
        ticker = self._ticker
        if ticker is None:
            return
        reactor = _kite_reactor()
        if reactor is not None and reactor.running:
            reactor.callFromThread(ticker.close)
        else:
            await asyncio.to_thread(ticker.close)

    def recovers_in_place(self, error: Exception) -> bool:
        """KiteTicker reconnects by itself after a drop and resubscribes on connect.

        Stopping it on a routine close cancels that retry (``close`` calls
        ``stop_retry``), which is what turned one dropped connection into a dead feed.
        Only its give-up, after ``reconnect_max_tries``, needs a new connection.
        """
        return not isinstance(error, TickerGaveUp)

    def _on_connect(self, ws: Any, response: Any) -> None:
        _ = response
        ws.subscribe(list(self.instrument_tokens))
        ws.set_mode(ws.MODE_FULL, list(self.instrument_tokens))

    def _on_ticks(self, ws: Any, ticks: list[dict[str, Any]]) -> None:
        _ = ws
        if self._loop is None:
            return
        for payload in ticks:
            symbol = "unmapped"
            try:
                token = payload["instrument_token"]
                if type(token) is not int:
                    raise ValueError("invalid_instrument_token")
                symbol = self.symbol_by_token.get(token, "unmapped")
                if symbol == "unmapped":
                    self.buffer.reject("unmapped_tick", symbol)
                    continue
                observed = _coerce_time(payload.get("exchange_timestamp"))
                depth = payload.get("depth") or {}
                bids = depth.get("buy") or []
                asks = depth.get("sell") or []
                tick = LiveTick(
                    symbol=symbol,
                    ltp=Decimal(str(payload.get("last_price", 0))),
                    volume=Decimal(str(payload.get("volume_traded", payload.get("volume", 0)))),
                    bid=_depth_price(bids), ask=_depth_price(asks), observed_at=observed, source="zerodha",
                )
                book = OrderBookUpdate(symbol, _depth_levels(bids), _depth_levels(asks), observed, "zerodha") if bids or asks else None
            except (ValueError, TypeError, KeyError, AttributeError, InvalidOperation, OverflowError):
                self.buffer.reject("invalid_zerodha_payload", symbol)
                continue
            asyncio.run_coroutine_threadsafe(self.on_tick(tick), self._loop)
            if book:
                asyncio.run_coroutine_threadsafe(self.on_orderbook_update(book), self._loop)

    def _on_error(self, ws: Any, code: Any, reason: Any) -> None:
        _ = ws
        if self._loop is not None:
            error = ConnectionError(f"KiteTicker error {code}: {reason}")
            asyncio.run_coroutine_threadsafe(self.on_connection_error(error), self._loop)

    def _on_close(self, ws: Any, code: Any, reason: Any) -> None:
        _ = ws
        if self._loop is not None:
            error = ConnectionError(f"KiteTicker closed {code}: {reason}")
            asyncio.run_coroutine_threadsafe(self.on_connection_error(error), self._loop)

    def _on_noreconnect(self, ws: Any) -> None:
        _ = ws
        if self._loop is not None:
            error = TickerGaveUp("KiteTicker stopped reconnecting after its maximum retries")
            asyncio.run_coroutine_threadsafe(self.on_connection_error(error), self._loop)


class IBKRAsyncTicker(AbstractTickerStream):
    def __init__(
        self,
        ib: Any,
        contracts: Iterable[Any],
        buffer: TickBuffer | None = None,
        *,
        connect_host: str | None = None,
        connect_port: int = 7497,
        client_id: int = 17,
    ) -> None:
        super().__init__(buffer)
        self.ib = ib
        self.contracts = tuple(contracts)
        self.connect_host = connect_host
        self.connect_port = connect_port
        self.client_id = client_id
        self._subscriptions: list[Any] = []
        self._loop: asyncio.AbstractEventLoop | None = None

    @classmethod
    def with_client(cls, contracts: Iterable[Any], buffer: TickBuffer | None = None) -> IBKRAsyncTicker:
        module = import_module("ib_async")
        return cls(module.IB(), contracts, buffer)

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        if self.connect_host is not None and not self._is_connected():
            connect_async = getattr(self.ib, "connectAsync", None)
            if connect_async is None:
                raise ConnectionError("IBKR client does not support async connection")
            await connect_async(self.connect_host, self.connect_port, clientId=self.client_id)
        disconnected = getattr(self.ib, "disconnectedEvent", None)
        if disconnected is not None:
            disconnected += self._on_disconnect
        for contract in self.contracts:
            ticker = self.ib.reqMktData(contract, "", False, False)
            ticker.updateEvent += self._on_quote
            self._subscriptions.append(ticker)
            if hasattr(self.ib, "reqMktDepth"):
                depth = self.ib.reqMktDepth(contract)
                if hasattr(depth, "updateEvent"):
                    depth.updateEvent += self._on_depth

    async def stop(self) -> None:
        if self._is_connected():
            for contract in self.contracts:
                self.ib.cancelMktData(contract)
                if hasattr(self.ib, "cancelMktDepth"):
                    self.ib.cancelMktDepth(contract)
        disconnected = getattr(self.ib, "disconnectedEvent", None)
        if disconnected is not None:
            try:
                disconnected -= self._on_disconnect
            except (TypeError, ValueError):
                pass
        self._subscriptions.clear()

    def _is_connected(self) -> bool:
        probe = getattr(self.ib, "isConnected", None)
        return True if probe is None else bool(probe())

    def _on_quote(self, ticker: Any) -> None:
        symbol = _contract_symbol(ticker.contract)
        tick = LiveTick(
            symbol=symbol,
            ltp=_decimal_or_zero(getattr(ticker, "last", None)),
            volume=_decimal_or_zero(getattr(ticker, "volume", None)),
            bid=_decimal_or_none(getattr(ticker, "bid", None)),
            ask=_decimal_or_none(getattr(ticker, "ask", None)),
            observed_at=datetime.now(timezone.utc),
            source="ibkr",
        )
        self.buffer.put(tick)

    def _on_depth(self, ticker: Any) -> None:
        symbol = _contract_symbol(ticker.contract)
        bids = tuple(
            (Decimal(str(level.price)), Decimal(str(level.size)))
            for level in getattr(ticker, "domBids", ())
        )
        asks = tuple(
            (Decimal(str(level.price)), Decimal(str(level.size)))
            for level in getattr(ticker, "domAsks", ())
        )
        update = OrderBookUpdate(
            symbol=symbol,
            bids=bids,
            asks=asks,
            observed_at=datetime.now(timezone.utc),
            source="ibkr",
        )
        self._dispatch_orderbook(update)

    def _on_disconnect(self, *args: Any) -> None:
        _ = args
        if self._loop is not None:
            error = ConnectionError("IBKR market-data connection dropped")
            asyncio.run_coroutine_threadsafe(self.on_connection_error(error), self._loop)

    def _dispatch_orderbook(self, update: OrderBookUpdate) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self.on_orderbook_update(update))


def _kite_reactor() -> Any:
    """The Twisted reactor KiteTicker runs on, or None when the SDK cannot say."""
    return getattr(import_module("kiteconnect.ticker"), "reactor", None)


def _depth_price(levels: list[dict[str, Any]]) -> Decimal | None:
    if not levels:
        return None
    return Decimal(str(levels[0].get("price", 0)))


def _depth_levels(levels: list[dict[str, Any]]) -> tuple[tuple[Decimal, Decimal], ...]:
    return tuple(
        (Decimal(str(level.get("price", 0))), Decimal(str(level.get("quantity", 0))))
        for level in levels
    )


def _coerce_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        # Kite's SDK uses datetime.fromtimestamp(epoch), which returns HOST-local
        # naive time. astimezone reverses that conversion, including its DST fold.
        return value.astimezone(timezone.utc)
    raise ValueError("missing_exchange_timestamp")


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not result.is_finite():
        return None
    return result


def _decimal_or_zero(value: Any) -> Decimal:
    return _decimal_or_none(value) or Decimal(0)


def _contract_symbol(contract: Any) -> str:
    symbol = getattr(contract, "localSymbol", None) or getattr(contract, "symbol", None)
    return str(symbol or "UNKNOWN")
