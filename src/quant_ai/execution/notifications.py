from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from decimal import Decimal
from typing import Callable, Protocol

from quant_ai.execution.briefing import (
    FOUNDER_EXECUTION_BRIEF_HEADER,
    FounderExecutionBrief,
)
from quant_ai.notifications.trading import (
    AlertPriority,
    TradingAlertCode,
    TradingNotification,
)
from quant_ai.notifications.trading import (
    TradingNotificationDispatcher as BaseTradingNotificationDispatcher,
)

PRAMANA_ALERT_PREFIX = "[PRAMANA]"


def _pramana_message(message: str) -> str:
    return message if message.startswith(PRAMANA_ALERT_PREFIX) else f"{PRAMANA_ALERT_PREFIX} {message}"


class NotificationChannel(Protocol):
    def send(self, notification: TradingNotification) -> None: ...


class ConsoleNotificationAdapter:
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.logger = logger or logging.getLogger("quant_ai.founder_briefs")

    def send(self, notification: TradingNotification) -> None:
        self.logger.info(
            json.dumps(
                {
                    "code": notification.code.value,
                    "message": _pramana_message(notification.message),
                    "tenant_id": notification.tenant_id,
                    "metadata": notification.metadata,
                },
                sort_keys=True,
            )
        )


class TelegramNotificationAdapter:
    """Optional bounded Telegram sender; secrets are constructor-injected only."""

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        *,
        timeout_seconds: float = 5.0,
        opener: Callable[..., object] = urllib.request.urlopen,
    ) -> None:
        if not bot_token or not chat_id:
            raise ValueError("telegram bot token and chat id are required")
        if not 0 < timeout_seconds <= 5:
            raise ValueError("telegram timeout must be within 5 seconds")
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout_seconds = timeout_seconds
        self.opener = opener

    def send(self, notification: TradingNotification) -> None:
        payload = urllib.parse.urlencode(
            {"chat_id": self.chat_id, "text": _pramana_message(notification.message)}
        ).encode()
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
            data=payload,
            method="POST",
        )
        response = self.opener(request, timeout=self.timeout_seconds)
        close = getattr(response, "close", None)
        if callable(close):
            close()


def format_founder_execution_brief(
    brief: FounderExecutionBrief,
    *,
    total_equity: Decimal,
    realized_pnl: Decimal,
    unrealized_pnl: Decimal,
    drawdown_fraction: Decimal,
) -> str:
    consensus = ", ".join(brief.swarm_consensus) or "none"
    trades = ", ".join(brief.paper_order_ids) or "none"
    return "\n".join(
        (
            FOUNDER_EXECUTION_BRIEF_HEADER,
            f"Generated: {brief.generated_at.isoformat()}",
            f"Market: {brief.market_state.value} | {brief.subject}",
            f"Allocation stance: {brief.mode}",
            f"Swarm: {consensus}",
            f"Risk: {brief.risk_decision}",
            f"Regime: {brief.market_regime} | Stress: {brief.stress_verdict}",
            f"Market-return Sharpe: {brief.sharpe_ratio} | Sortino: {brief.sortino_ratio} (not strategy performance)",
            f"XAI: {'; '.join(brief.xai_rationales) or 'none'}",
            f"Trades: {trades}",
            f"Equity: {total_equity} | Realized P&L: {realized_pnl}",
            f"Unrealized P&L: {unrealized_pnl} | Drawdown: {drawdown_fraction}",
        )
    )


class TradingNotificationDispatcher(BaseTradingNotificationDispatcher):
    def dispatch_brief(
        self,
        brief: FounderExecutionBrief,
        *,
        total_equity: Decimal,
        realized_pnl: Decimal,
        unrealized_pnl: Decimal,
        drawdown_fraction: Decimal,
        tenant_id: str = "default",
    ) -> TradingNotification:
        message = format_founder_execution_brief(
            brief,
            total_equity=total_equity,
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized_pnl,
            drawdown_fraction=drawdown_fraction,
        )
        return self.dispatch(
            TradingAlertCode.CADENCE_BRIEF,
            message,
            tenant_id=tenant_id,
            priority=AlertPriority.INFO,
            metadata={
                "market_state": brief.market_state.value,
                "mode": brief.mode,
                "paper_orders": str(len(brief.paper_order_ids)),
            },
        )
