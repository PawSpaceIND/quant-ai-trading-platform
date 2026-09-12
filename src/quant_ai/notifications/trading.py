from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol
from uuid import uuid4


class TradingAlertCode(str, Enum):
    MAX_DRAWDOWN_BREACHED = "MAX_DRAWDOWN_BREACHED"
    KILL_SWITCH_ENGAGED = "KILL_SWITCH_ENGAGED"
    DAILY_LOSS_LIMIT_BREACHED = "DAILY_LOSS_LIMIT_BREACHED"
    PROVIDER_STALE = "PROVIDER_STALE"
    RISK_PROPOSAL_REJECTED = "RISK_PROPOSAL_REJECTED"
    CADENCE_BRIEF = "CADENCE_BRIEF"


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


class ConsoleLogSink:
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.logger = logger or logging.getLogger("quant_ai.trading_alerts")

    def send(self, notification: TradingNotification) -> None:
        payload = asdict(notification)
        payload["code"] = notification.code.value
        payload["priority"] = notification.priority.value
        payload["created_at"] = notification.created_at.isoformat()
        self.logger.critical(json.dumps(payload, sort_keys=True))


class TradingNotificationDispatcher:
    """High-priority founder alert outbox with pluggable delivery sinks."""

    def __init__(self, sinks: tuple[TradingNotificationSink, ...] | None = None) -> None:
        self.sinks = sinks or (ConsoleLogSink(),)
        self._outbox: list[TradingNotification] = []

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
            message,
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
