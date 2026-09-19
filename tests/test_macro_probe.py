"""The macro provider is asked once at boot, and a refusal is loud, durable and key-free."""
from __future__ import annotations

import inspect
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from quant_ai.intelligence.providers import MacroSnapshot
from quant_ai.intelligence.resilience import ProviderHttpError
from quant_ai.notifications.trading import (
    AlertPriority,
    TradingAlertCode,
    TradingNotificationDispatcher,
)
from quant_ai.operations import macro_probe
from quant_ai.operations.macro_probe import MacroProbeResult, check_macro_provider

NOW = datetime(2026, 9, 21, 3, 0, tzinfo=timezone.utc)
KEY = "fake-fred-key"
ENV = {"FRED_API_KEY": KEY, "PRAMANA_TENANT_ID": "ghost"}


class CaptureSink:
    def __init__(self) -> None:
        self.sent = []

    def send(self, notification) -> None:
        self.sent.append(notification)


class FakeFred:
    """Stands in for FredMacroProvider: either answers with a snapshot or raises."""

    def __init__(self, *, snapshot=None, error=None) -> None:
        self.snapshot = snapshot
        self.error = error
        self.calls = 0

    def fetch(self, indicators, now):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.snapshot


def _dispatcher() -> tuple[TradingNotificationDispatcher, CaptureSink]:
    sink = CaptureSink()
    return TradingNotificationDispatcher((sink,)), sink


def test_rejected_key_alerts_critically_and_does_not_stop_the_boot() -> None:
    """The failure that cost two sessions: FRED answers 400 to a rotated key.

    The probe must turn that into a CRITICAL durable alert with a fixed reason - and return,
    because macro is optional and a degraded pilot is still a safe one. Raising here would
    trade a silent failure for a pilot that cannot start, which is the wrong direction.
    """
    dispatcher, sink = _dispatcher()
    fake = FakeFred(error=ProviderHttpError(400, "provider_request_rejected"))
    result = check_macro_provider(env=ENV, now=NOW, dispatcher=dispatcher, provider_factory=lambda key: fake)
    assert result == MacroProbeResult(True, False, "fred_key_rejected", 400)
    assert len(sink.sent) == 1
    alert = sink.sent[0]
    assert alert.code is TradingAlertCode.MACRO_PROVIDER_UNAVAILABLE
    assert alert.priority is AlertPriority.CRITICAL
    assert alert.tenant_id == "ghost"
    assert alert.metadata == {"phase": "boot", "reason": "fred_key_rejected", "status": "400"}


@pytest.mark.parametrize("status,reason", [(429, "fred_rate_limited"), (500, "fred_unavailable"),
                                           (503, "fred_unavailable"), (401, "fred_key_rejected"),
                                           (403, "fred_key_rejected")])
def test_status_codes_map_to_fixed_reasons(status: int, reason: str) -> None:
    dispatcher, sink = _dispatcher()
    fake = FakeFred(error=ProviderHttpError(status, "x"))
    result = check_macro_provider(env=ENV, now=NOW, dispatcher=dispatcher, provider_factory=lambda key: fake)
    assert (result.healthy, result.reason, result.status_code) == (False, reason, status)
    assert sink.sent[0].metadata["reason"] == reason


def test_healthy_provider_is_silent() -> None:
    dispatcher, sink = _dispatcher()
    fake = FakeFred(snapshot=MacroSnapshot({"US10Y": Decimal("4.1")}, NOW))
    result = check_macro_provider(env=ENV, now=NOW, dispatcher=dispatcher, provider_factory=lambda key: fake)
    assert result == MacroProbeResult(True, True, "ok")
    assert fake.calls == 1
    assert sink.sent == []


def test_unset_key_makes_no_request_and_no_alert() -> None:
    """Absent by choice is not a fault, and must not even build a client."""
    dispatcher, sink = _dispatcher()

    def forbidden(key):
        raise AssertionError("provider must not be constructed without a key")

    result = check_macro_provider(env={"PRAMANA_TENANT_ID": "ghost"}, now=NOW,
                                  dispatcher=dispatcher, provider_factory=forbidden)
    assert result == MacroProbeResult(False, False, "macro_not_configured")
    assert sink.sent == []


def test_empty_snapshot_is_a_failure_not_a_pass() -> None:
    """A 200 with no observations is what a wrong series id or a blank account returns."""
    dispatcher, sink = _dispatcher()
    fake = FakeFred(snapshot=MacroSnapshot({}, NOW))
    result = check_macro_provider(env=ENV, now=NOW, dispatcher=dispatcher, provider_factory=lambda key: fake)
    assert result.reason == "fred_no_observations" and result.healthy is False
    assert len(sink.sent) == 1


def test_transport_failure_names_the_type_and_never_the_message() -> None:
    """A transport error can echo the request URL, and the URL carries the key."""
    dispatcher, sink = _dispatcher()
    fake = FakeFred(error=OSError(f"https://api.stlouisfed.org/...?api_key={KEY}"))
    result = check_macro_provider(env=ENV, now=NOW, dispatcher=dispatcher, provider_factory=lambda key: fake)
    assert result.reason == "fred_probe_failed:OSError"
    blob = repr(sink.sent[0])
    assert KEY not in blob and KEY not in result.reason


def test_key_never_appears_in_any_alert_field() -> None:
    dispatcher, sink = _dispatcher()
    fake = FakeFred(error=ProviderHttpError(400, f"rejected api_key={KEY}"))
    check_macro_provider(env=ENV, now=NOW, dispatcher=dispatcher, provider_factory=lambda key: fake)
    assert KEY not in repr(sink.sent[0])


def test_invalid_phase_is_refused() -> None:
    with pytest.raises(ValueError, match="macro_probe_phase_invalid"):
        check_macro_provider(env=ENV, now=NOW, phase="whenever", provider_factory=lambda key: FakeFred())


def test_ghost_boot_probes_macro_right_after_the_token() -> None:
    """Pins the wiring: the probe runs in the same boot path as the token check.

    A probe that exists but is never called is the failure mode this whole change exists
    to prevent, so the call site is asserted rather than assumed.
    """
    from quant_ai import daemon

    source = inspect.getsource(daemon.build_ghost_runner_from_env)
    token_at = source.index("check_runtime_token(dispatcher=dispatcher)")
    probe_at = source.index("check_macro_provider(dispatcher=dispatcher)")
    assert token_at < probe_at
    assert macro_probe.PROBE_INDICATOR == "US10Y"
