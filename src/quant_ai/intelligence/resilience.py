from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from enum import Enum
from threading import Lock
from typing import Callable, Protocol


class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitOpenError(RuntimeError):
    pass


class ProviderHttpError(RuntimeError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class PayloadTooLargeError(RuntimeError):
    pass


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    body: bytes
    headers: dict[str, str]


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None,
        headers: dict[str, str] | None,
        timeout_seconds: float,
        max_bytes: int,
    ) -> HttpResponse: ...


class UrllibTransport:
    """Bounded standard-library transport used by read-only provider adapters."""

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None,
        headers: dict[str, str] | None,
        timeout_seconds: float,
        max_bytes: int,
    ) -> HttpResponse:
        query = urllib.parse.urlencode(params or {})
        target = f"{url}?{query}" if query else url
        request = urllib.request.Request(target, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                body = response.read(max_bytes + 1)
                if len(body) > max_bytes:
                    raise PayloadTooLargeError("provider_payload_exceeds_limit")
                return HttpResponse(
                    int(response.status),
                    body,
                    {key.lower(): value for key, value in response.headers.items()},
                )
        except urllib.error.HTTPError as exc:
            body = exc.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise PayloadTooLargeError("provider_payload_exceeds_limit") from exc
            return HttpResponse(
                int(exc.code),
                body,
                {key.lower(): value for key, value in exc.headers.items()},
            )


class TokenBucketRateLimiter:
    MAX_OPS = 10.0
    def __init__(
        self,
        rate_per_second: float,
        capacity: float = 1.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate_per_second <= 0 or capacity <= 0:
            raise ValueError("rate and capacity must be positive")
        self.rate = min(rate_per_second, self.MAX_OPS)
        self.capacity = min(capacity, 1.0)
        self.clock = clock
        self.sleeper = sleeper
        self._tokens = self.capacity
        self._updated_at = clock()
        self._lock = Lock()

    def acquire(self, tokens: float = 1.0) -> float:
        if tokens <= 0 or tokens > self.capacity:
            raise ValueError("requested tokens must be within bucket capacity")
        with self._lock:
            now = self.clock()
            elapsed = max(0.0, now - self._updated_at)
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._updated_at = now
            if self._tokens >= tokens:
                self._tokens -= tokens
                return 0.0
            wait = (tokens - self._tokens) / self.rate
        self.sleeper(wait)
        with self._lock:
            now = self.clock()
            elapsed = max(0.0, now - self._updated_at)
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._updated_at = now
            self._tokens = max(0.0, self._tokens - tokens)
        return wait


class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout_seconds: float = 30.0,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold <= 0 or recovery_timeout_seconds <= 0:
            raise ValueError("failure threshold and recovery timeout must be positive")
        self.failure_threshold = failure_threshold
        self.recovery_timeout_seconds = recovery_timeout_seconds
        self.clock = clock
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.opened_at: float | None = None

    def before_request(self) -> None:
        if self.state == CircuitState.OPEN:
            assert self.opened_at is not None
            if self.clock() - self.opened_at < self.recovery_timeout_seconds:
                raise CircuitOpenError("provider_circuit_open")
            self.state = CircuitState.HALF_OPEN

    def record_success(self) -> None:
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.failure_count += 1
        if self.state == CircuitState.HALF_OPEN or self.failure_count >= self.failure_threshold:
            self.force_open()

    def force_open(self) -> None:
        self.state = CircuitState.OPEN
        self.opened_at = self.clock()


class ResilientHttpClient:
    """Read-only HTTP guard: rate-limit, circuit, timeout, retries and size bound."""

    def __init__(
        self,
        transport: HttpTransport,
        *,
        rate_limiter: TokenBucketRateLimiter | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        timeout_seconds: float = 5.0,
        max_attempts: int = 3,
        max_payload_bytes: int = 1_000_000,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if not 0 < timeout_seconds <= 5:
            raise ValueError("timeout_seconds must be within (0, 5]")
        if not 1 <= max_attempts <= 3:
            raise ValueError("max_attempts must be within [1, 3]")
        if max_payload_bytes <= 0:
            raise ValueError("max_payload_bytes must be positive")
        self.transport = transport
        self.rate_limiter = rate_limiter or TokenBucketRateLimiter(5.0, 5.0)
        self.circuit_breaker = circuit_breaker or CircuitBreaker()
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.max_payload_bytes = max_payload_bytes
        self.sleeper = sleeper

    def get_response(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        accept_statuses: frozenset[int] = frozenset(),
    ) -> HttpResponse:
        """Guarded GET returning status, body and headers.

        ``accept_statuses`` lists 4xx codes that are the endpoint's normal answer rather than
        a failure (Yahoo's cookie bootstrap replies 404 while setting the session cookie).
        429 and 5xx are always failures.
        """
        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
            self.circuit_breaker.before_request()
            self.rate_limiter.acquire()
            try:
                response = self.transport.request(
                    "GET",
                    url,
                    params=params,
                    headers=headers,
                    timeout_seconds=self.timeout_seconds,
                    max_bytes=self.max_payload_bytes,
                )
                if response.status_code == 429:
                    self.circuit_breaker.force_open()
                    raise ProviderHttpError(429, "provider_rate_limited")
                if response.status_code >= 500:
                    raise ProviderHttpError(response.status_code, "provider_server_error")
                if response.status_code >= 400 and response.status_code not in accept_statuses:
                    raise ProviderHttpError(response.status_code, "provider_request_rejected")
                if len(response.body) > self.max_payload_bytes:
                    raise PayloadTooLargeError("provider_payload_exceeds_limit")
                self.circuit_breaker.record_success()
                return response
            except (TimeoutError, OSError, PayloadTooLargeError, ProviderHttpError) as exc:
                last_error = exc
                if isinstance(exc, ProviderHttpError) and exc.status_code == 429:
                    break
                if not isinstance(exc, ProviderHttpError) or exc.status_code >= 500:
                    self.circuit_breaker.record_failure()
                if self.circuit_breaker.state == CircuitState.OPEN:
                    break
                if attempt + 1 < self.max_attempts:
                    self.sleeper(0.1 * (2**attempt))
        assert last_error is not None
        raise last_error

    def get_bytes(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        return self.get_response(url, params=params, headers=headers).body

    def get_json(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> object:
        return json.loads(self.get_bytes(url, params=params, headers=headers).decode("utf-8"))

    def get_text(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> str:
        return self.get_bytes(url, params=params, headers=headers).decode("utf-8")
