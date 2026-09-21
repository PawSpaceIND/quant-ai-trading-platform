"""FRED serves its last good daily reading through a slow or failing request.

On 21 September 2026 the pilot host logged three FRED timeouts and three open-circuit
refusals in two hours. Each one failed the whole fetch, the composite dropped the FRED part,
and the core indicators were blank for that decision even though the daily series behind
them had not changed since the previous successful fetch minutes earlier.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant_ai.governance.runtime_manifest import describe
from quant_ai.intelligence.external.fred import FRED_FAILURES, FredMacroProvider, FredUnavailable
from quant_ai.intelligence.external.india_macro import PART_FAILURES, CompositeMacroProvider
from quant_ai.intelligence.providers import MACRO_CORE_INDICATORS
from quant_ai.intelligence.resilience import (
    HttpResponse,
    ResilientHttpClient,
    TokenBucketRateLimiter,
    UrllibTransport,
)

NOW = datetime(2026, 9, 21, 4, 30, tzinfo=timezone.utc)
LOGGER = "quant_ai.fred_macro"
CORE = MACRO_CORE_INDICATORS


class ScriptedTransport:
    """Answers every series from ``observations``; raises when ``failing`` is set."""

    def __init__(self, observations: dict[str, str] | None = None) -> None:
        self.observations = observations or {"DGS10": "4.25", "DCOILBRENTEU": "66.10", "NASDAQQGLDI": "3140.5", "DTWEXBGS": "121.3"}
        self.failing: type[BaseException] | None = None
        self.requests: list[str] = []
        self.date = "2026-09-18"

    def request(self, method, url, *, params, headers, timeout_seconds, max_bytes):
        self.requests.append(params["series_id"])
        if self.failing is not None:
            raise self.failing("scripted")
        value = self.observations[params["series_id"]]
        body = json.dumps({"observations": [{"date": self.date, "value": value}]}).encode()
        return HttpResponse(200, body, {})


def provider(transport: ScriptedTransport, **kwargs) -> FredMacroProvider:
    client = ResilientHttpClient(
        transport,
        rate_limiter=TokenBucketRateLimiter(100.0, 100.0, sleeper=lambda _: None),
        max_attempts=1,
    )
    return FredMacroProvider(client, "unit-test-not-a-key", **kwargs)


def test_failure_set_matches_what_the_composite_treats_as_a_part_failure() -> None:
    assert FRED_FAILURES == PART_FAILURES


def test_a_success_is_held_for_cache_ttl_and_refetched_after_it() -> None:
    transport = ScriptedTransport()
    fred = provider(transport)
    first = fred.fetch(CORE, NOW)
    assert first.indicators["US10Y"] == Decimal("4.25")
    assert len(transport.requests) == 4
    for minutes in (1, 9, 14):
        assert fred.fetch(CORE, NOW + timedelta(minutes=minutes)) is first
    assert len(transport.requests) == 4
    transport.observations["DGS10"] = "4.30"
    again = fred.fetch(CORE, NOW + timedelta(minutes=15))
    assert again.indicators["US10Y"] == Decimal("4.30")
    assert len(transport.requests) == 8


def test_a_timeout_after_a_success_serves_the_held_snapshot_with_its_own_stamps(caplog) -> None:
    transport = ScriptedTransport()
    fred = provider(transport)
    good = fred.fetch(CORE, NOW)
    transport.failing = TimeoutError
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        held = fred.fetch(CORE, NOW + timedelta(minutes=16))
    assert held is good
    assert held.observed_at == datetime(2026, 9, 18, tzinfo=timezone.utc)
    assert len(transport.requests) == 5  # one probe, not four
    warnings = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert warnings == ["fred_unavailable error=TimeoutError held_age_seconds=960"]


def test_a_failure_is_held_for_abstain_ttl_so_the_source_is_probed_once() -> None:
    transport = ScriptedTransport()
    fred = provider(transport)
    fred.fetch(CORE, NOW)
    transport.failing = OSError
    fred.fetch(CORE, NOW + timedelta(minutes=16))
    probes = len(transport.requests)
    for seconds in (10, 60, 119):
        fred.fetch(CORE, NOW + timedelta(minutes=16, seconds=seconds))
    assert len(transport.requests) == probes
    transport.failing = None
    transport.observations["DGS10"] = "4.40"
    recovered = fred.fetch(CORE, NOW + timedelta(minutes=18))
    assert recovered.indicators["US10Y"] == Decimal("4.40")
    assert len(transport.requests) == probes + 4


def test_a_failure_with_nothing_held_is_raised_for_the_composite_to_log() -> None:
    transport = ScriptedTransport()
    transport.failing = TimeoutError
    fred = provider(transport)
    with pytest.raises(TimeoutError):
        fred.fetch(CORE, NOW)
    # Inside the back-off with nothing good to serve, the refusal names the hold.
    with pytest.raises(FredUnavailable, match="fred_backing_off"):
        fred.fetch(CORE, NOW + timedelta(seconds=30))
    assert len(transport.requests) == 1


def test_a_held_snapshot_older_than_the_grace_is_not_served() -> None:
    transport = ScriptedTransport()
    fred = provider(transport, cache_ttl=timedelta(minutes=15), stale_grace=timedelta(hours=1))
    fred.fetch(CORE, NOW)
    transport.failing = TimeoutError
    assert fred.fetch(CORE, NOW + timedelta(minutes=59)).indicators["US10Y"] == Decimal("4.25")
    with pytest.raises(TimeoutError):
        fred.fetch(CORE, NOW + timedelta(hours=1, minutes=2))


def test_data_errors_hold_through_too_but_never_on_a_first_fetch() -> None:
    transport = ScriptedTransport()
    fred = provider(transport)
    transport.observations["DGS10"] = "not-a-number"
    with pytest.raises(ValueError, match="fred_invalid_observation_value"):
        fred.fetch(CORE, NOW)
    transport.observations["DGS10"] = "4.25"
    good = fred.fetch(CORE, NOW + timedelta(minutes=3))
    transport.observations["DGS10"] = "NaN"
    assert fred.fetch(CORE, NOW + timedelta(minutes=20)) is good


def test_indicator_sets_are_held_separately() -> None:
    transport = ScriptedTransport()
    fred = provider(transport)
    core = fred.fetch(CORE, NOW)
    only_yield = fred.fetch(("US10Y",), NOW)
    assert set(core.indicators) == set(CORE)
    assert set(only_yield.indicators) == {"US10Y"}
    assert len(transport.requests) == 5
    assert fred.fetch(("US10Y", "US10Y"), NOW + timedelta(minutes=1)) is only_yield


def test_a_clock_going_backwards_refetches_rather_than_serving_the_future() -> None:
    transport = ScriptedTransport()
    fred = provider(transport)
    fred.fetch(CORE, NOW)
    fred.fetch(CORE, NOW - timedelta(minutes=1))
    assert len(transport.requests) == 8


def test_naive_clock_is_refused_before_any_hold_or_request() -> None:
    transport = ScriptedTransport()
    fred = provider(transport)
    fred.fetch(CORE, NOW)
    with pytest.raises(ValueError, match="fred_clock_must_be_aware"):
        fred.fetch(CORE, NOW.replace(tzinfo=None))


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"cache_ttl": timedelta(0)}, "fred_ttls_must_be_positive"),
        ({"abstain_ttl": timedelta(seconds=-1)}, "fred_ttls_must_be_positive"),
        ({"stale_grace": timedelta(0)}, "fred_ttls_must_be_positive"),
        ({"cache_ttl": timedelta(hours=2), "stale_grace": timedelta(hours=1)}, "fred_stale_grace_must_cover_cache_ttl"),
    ],
)
def test_constructor_refuses_impossible_windows(kwargs, message) -> None:
    with pytest.raises(ValueError, match=message):
        provider(ScriptedTransport(), **kwargs)


def test_defaults_are_the_documented_windows_and_the_manifest_carries_them() -> None:
    fred = provider(ScriptedTransport())
    assert (fred.cache_ttl, fred.abstain_ttl, fred.stale_grace) == (
        timedelta(minutes=15),
        timedelta(minutes=2),
        timedelta(hours=6),
    )
    # The manifest describes the production transport; the scripted one is for the tests above.
    production = FredMacroProvider(ResilientHttpClient(UrllibTransport()), "unit-test-not-a-key")
    issues: list[str] = []
    described = describe(production, issues)
    assert issues == []
    assert described is not None
    for name in ("cache_ttl", "abstain_ttl", "stale_grace"):
        assert name in described["parameters"], name


def test_composite_keeps_the_core_through_a_fred_timeout(caplog) -> None:
    transport = ScriptedTransport()
    composite = CompositeMacroProvider((provider(transport),))
    first = composite.fetch(CORE, NOW)
    assert set(first.indicators) == set(CORE)
    transport.failing = TimeoutError
    with caplog.at_level(logging.WARNING):
        held = composite.fetch(CORE, NOW + timedelta(minutes=16))
    assert held.indicators == first.indicators
    assert held.observed_at == first.observed_at
    messages = [record.getMessage() for record in caplog.records]
    assert not any(message.startswith("macro_part_unavailable") for message in messages)
    assert any(message.startswith("fred_unavailable error=TimeoutError") for message in messages)
