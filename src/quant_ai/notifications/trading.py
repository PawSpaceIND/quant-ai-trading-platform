from __future__ import annotations

import json
import logging
import os
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

# The outbox is an in-process convenience for the current session, not the record of
# record: the JSON-lines alert log is. Keeping the newest few thousand alerts bounds a
# daemon that is expected to run for months, and every consumer reads the tail.
RETAINED_NOTIFICATIONS = 5_000


class TradingAlertCode(str, Enum):
    MAX_DRAWDOWN_BREACHED = "MAX_DRAWDOWN_BREACHED"
    KILL_SWITCH_ENGAGED = "KILL_SWITCH_ENGAGED"
    DAILY_LOSS_LIMIT_BREACHED = "DAILY_LOSS_LIMIT_BREACHED"
    PROVIDER_STALE = "PROVIDER_STALE"
    ZERODHA_SESSION_INVALID = "ZERODHA_SESSION_INVALID"
    RISK_PROPOSAL_REJECTED = "RISK_PROPOSAL_REJECTED"
    CADENCE_BRIEF = "CADENCE_BRIEF"
    STOP_LOSS_TRIGGERED = "STOP_LOSS_TRIGGERED"
    TAKE_PROFIT_TRIGGERED = "TAKE_PROFIT_TRIGGERED"
    SESSION_FLATTENED = "SESSION_FLATTENED"
    CADENCE_TICK_FAILED = "CADENCE_TICK_FAILED"
    LEARNING_EVIDENCE_DEGRADED = "LEARNING_EVIDENCE_DEGRADED"
    # A price step across a session boundary that the engine could not explain. Its own
    # code because an operator has to be able to find these without reading every stop
    # alert: they are the cases where a corporate action and a catastrophic gap are
    # indistinguishable, and only a human can close them.
    OVERNIGHT_GAP_UNEXPLAINED = "OVERNIGHT_GAP_UNEXPLAINED"


class AlertPriority(str, Enum):
    INFO = "INFO"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True)
class TradingNotification:
    notification_id: str
    tenant_id: str
    code: TradingAlertCode
    priority: AlertPriority
    message: str
    created_at: datetime
    metadata: dict[str, str]


class TradingNotificationSink(Protocol):
    def send(self, notification: TradingNotification) -> None: ...


def alert_payload(notification: TradingNotification) -> dict[str, Any]:
    """The JSON-ready projection of an alert, shared by every sink."""
    payload = asdict(notification)
    payload["code"] = notification.code.value
    payload["priority"] = notification.priority.value
    payload["created_at"] = notification.created_at.isoformat()
    return payload


class ConsoleLogSink:
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.logger = logger or logging.getLogger("quant_ai.trading_alerts")

    def send(self, notification: TradingNotification) -> None:
        self.logger.critical(json.dumps(alert_payload(notification), sort_keys=True))


class JsonlFileSink:
    """Append every alert as one JSON line to a file on the shared data volume.

    Needs no credentials, so it can always be wired in. The console sink writes to
    stderr, which dies with the container the documented upgrade replaces; this file
    lives in the volume and survives it.

    A write failure is logged and swallowed. The alerts worth reading are the ones
    raised while something is already wrong - a sink that raised would abort the kill
    switch or the cadence tick that is trying to report the problem.
    """

    def __init__(self, path: str | Path, logger: logging.Logger | None = None) -> None:
        self.path = Path(path)
        self.logger = logger or logging.getLogger("quant_ai.trading_alerts")

    def send(self, notification: TradingNotification) -> None:
        try:
            payload = alert_payload(notification)
            # When the alert was written down, distinct from when it was raised.
            payload["logged_at"] = datetime.now(timezone.utc).isoformat()
            line = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Private from the first byte, append-only, and never truncating: an
            # existing log keeps its own mode.
            descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                handle.write(line)
        except Exception:  # deliberately broad: no alert sink may break the cadence
            self.logger.exception("alert_log_write_failed path=%s", self.path)


class TradingNotificationDispatcher:
    """High-priority founder alert outbox with pluggable delivery sinks."""

    def __init__(
        self,
        sinks: tuple[TradingNotificationSink, ...] | None = None,
        *,
        retained: int = RETAINED_NOTIFICATIONS,
    ) -> None:
        if retained < 1:
            raise ValueError("retained notifications must be positive")
        self.sinks = sinks or (ConsoleLogSink(),)
        self._outbox: deque[TradingNotification] = deque(maxlen=retained)

    def dispatch(
        self,
        code: TradingAlertCode,
        message: str,
        *,
        tenant_id: str = "default",
        priority: AlertPriority = AlertPriority.CRITICAL,
        metadata: dict[str, str] | None = None,
    ) -> TradingNotification:
        if not message.strip():
            raise ValueError("notification message is required")
        notification = TradingNotification(
            uuid4().hex,
            tenant_id,
            code,
            priority,
            message if message.startswith("[PRAMANA]") else f"[PRAMANA] {message}",
            datetime.now(timezone.utc),
            metadata or {},
        )
        self._outbox.append(notification)
        for sink in self.sinks:
            sink.send(notification)
        return notification

    def pending(self, tenant_id: str | None = None) -> tuple[TradingNotification, ...]:
        if tenant_id is None:
            return tuple(self._outbox)
        return tuple(item for item in self._outbox if item.tenant_id == tenant_id)
