from __future__ import annotations

from collections import deque

import pytest

from quant_ai.intelligence.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    HttpResponse,
    ProviderHttpError,
    ResilientHttpClient,
    TokenBucketRateLimiter,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class SequenceTransport:
    def __init__(self, outcomes) -> None:
        self.outcomes = deque(outcomes)
        self.calls = 0

    def request(self, method, url, *, params, headers, timeout_seconds, max_bytes):
        self.calls += 1
        outcome = self.outcomes.popleft()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_token_bucket_enforces_delay() -> None:
    clock = FakeClock()
    limiter = TokenBucketRateLimiter(2.0, 1.0, clock=clock, sleeper=clock.sleep)
    assert limiter.acquire() == 0
    assert limiter.acquire() == pytest.approx(0.5)
    assert clock.value == pytest.approx(0.5)


def test_circuit_breaker_opens_and_half_open_canary() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(2, 10, clock=clock)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == CircuitState.OPEN
    with pytest.raises(CircuitOpenError):
        breaker.before_request()
    clock.value = 11
    breaker.before_request()
    assert breaker.state == CircuitState.HALF_OPEN
    breaker.record_success()
    assert breaker.state == CircuitState.CLOSED


def test_http_repeated_500_trips_circuit() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(2, 30, clock=clock)
    transport = SequenceTransport([
        HttpResponse(500, b"x", {}),
        HttpResponse(500, b"x", {}),
    ])
    client = ResilientHttpClient(
        transport,
        circuit_breaker=breaker,
        rate_limiter=TokenBucketRateLimiter(100, 3, clock=clock, sleeper=clock.sleep),
        max_attempts=3,
        sleeper=clock.sleep,
    )
    with pytest.raises(ProviderHttpError):
        client.get_bytes("https://provider.invalid")
    assert breaker.state == CircuitState.OPEN
    assert transport.calls == 2


def test_http_429_immediately_trips_circuit() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(3, 30, clock=clock)
    client = ResilientHttpClient(
        SequenceTransport([HttpResponse(429, b"", {})]),
        circuit_breaker=breaker,
        rate_limiter=TokenBucketRateLimiter(100, 3, clock=clock, sleeper=clock.sleep),
        sleeper=clock.sleep,
    )
    with pytest.raises(ProviderHttpError):
        client.get_bytes("https://provider.invalid")
    assert breaker.state == CircuitState.OPEN
