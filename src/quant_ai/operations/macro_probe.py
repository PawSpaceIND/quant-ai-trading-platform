"""Boot-time check that the configured macro provider actually answers.

Nothing else in the system will tell the operator when FRED stops answering. The failover
layer turns every provider error into an empty ``MacroSnapshot``; the freshness gate turns an
empty snapshot into ``MISSING`` and a confidence multiplier of zero; every specialist that
declares macro as a dependency then reports zero, the consensus mean sinks below its floor,
and the pilot holds all day. Each step is correct on its own. Together they hide a rejected
API key behind a stream of legitimate-looking NEUTRAL decisions - which is how a key rotated
after a credential leak, and never updated on the host, silenced three of five specialists for
two full sessions with nothing in the logs.

So the daemon asks once at boot, the way it asks Kite whether the access token is real. Unlike
the token check this never refuses to start: macro is an optional input and a pilot without it
is degraded, not unsafe. It logs, dispatches a CRITICAL alert to the durable outbox, and hands
back a fixed credential-free reason. The key itself never appears in any output.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from quant_ai.intelligence.external.fred import FredMacroProvider
from quant_ai.intelligence.resilience import ProviderHttpError, ResilientHttpClient, UrllibTransport
from quant_ai.notifications.trading import (
    AlertPriority,
    TradingAlertCode,
    TradingNotificationDispatcher,
)

LOGGER = logging.getLogger(__name__)

# One cheap series is enough to prove the key and the route. US10Y is on every macro request
# the pipeline makes, so a probe that passes here exercises the path the specialists use.
PROBE_INDICATOR = "US10Y"

PHASES = frozenset({"boot", "preopen", "watch_start"})


@dataclass(frozen=True)
class MacroProbeResult:
    configured: bool
    healthy: bool
    # Fixed, credential-free code safe for console and durable alerts.
    reason: str
    status_code: int | None = None


def _default_provider(api_key: str) -> Any:
    return FredMacroProvider(ResilientHttpClient(UrllibTransport()), api_key)


def _reason_for(status_code: int) -> str:
    if status_code == 429:
        return "fred_rate_limited"
    if status_code >= 500:
        return "fred_unavailable"
    # Everything else in 4xx is the request being refused - an invalid, expired or
    # mistyped api_key answers 400 from FRED. The one thing an operator can fix.
    return "fred_key_rejected"


def check_macro_provider(
    *,
    env: Mapping[str, str] | None = None,
    now: datetime | None = None,
    dispatcher: TradingNotificationDispatcher | None = None,
    provider_factory: Callable[[str], Any] | None = None,
    phase: str = "boot",
) -> MacroProbeResult:
    """Probe the macro provider once; alert on failure; never raise, never print the key."""
    if phase not in PHASES:
        raise ValueError("macro_probe_phase_invalid")
    source = os.environ if env is None else env
    api_key = source.get("FRED_API_KEY", "").strip()
    if not api_key:
        # Absent by choice is not a fault. The specialists that need macro will report zero
        # and say why in their own freshness diagnostic; there is nothing to alert about.
        return MacroProbeResult(False, False, "macro_not_configured")
    moment = now or datetime.now(timezone.utc)
    provider = (provider_factory or _default_provider)(api_key)
    try:
        snapshot = provider.fetch((PROBE_INDICATOR,), moment)
    except ProviderHttpError as error:
        result = MacroProbeResult(True, False, _reason_for(error.status_code), error.status_code)
    except (TimeoutError, OSError, RuntimeError, ValueError, TypeError, KeyError) as error:
        # The same family the failover registry swallows into AllProvidersFailed. Name the
        # type, never the message: a transport error can echo the request URL, and the URL
        # carries the key.
        result = MacroProbeResult(True, False, f"fred_probe_failed:{type(error).__name__}")
    else:
        if PROBE_INDICATOR in snapshot.indicators:
            LOGGER.info("macro_provider_probe_ok phase=%s", phase)
            return MacroProbeResult(True, True, "ok")
        result = MacroProbeResult(True, False, "fred_no_observations")
    LOGGER.error(
        "macro_provider_probe_failed phase=%s reason=%s status=%s",
        phase, result.reason, result.status_code,
    )
    if dispatcher is None:
        from quant_ai.operations.zerodha_renewal import notifications
        dispatcher = notifications()
    dispatcher.dispatch(
        TradingAlertCode.MACRO_PROVIDER_UNAVAILABLE,
        "Macro provider (FRED) is not answering. Every specialist that reads macro will report "
        "zero confidence and the consensus cannot reach its floor; the pilot will hold all "
        "session. Check FRED_API_KEY on the host and re-run the probe.",
        tenant_id=source.get("PRAMANA_TENANT_ID", "ghost"),
        priority=AlertPriority.CRITICAL,
        metadata={"phase": phase, "reason": result.reason,
                  "status": "" if result.status_code is None else str(result.status_code)},
    )
    return result
