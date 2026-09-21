from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Sequence
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
LOGGER = logging.getLogger("quant_ai.trading_alerts")
# Telegram refuses a message over 4,096 characters with HTTP 400; the margin leaves room
# for the prefix and the truncation marker.
TELEGRAM_MAX_CHARS = 4000
TELEGRAM_TRUNCATION_MARKER = " … [truncated; full record in the alert log]"
# Detail blocks a cycle digest carries before it points at the proofs instead.
MAX_DIGEST_DETAILS = 3


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
        """Best-effort delivery: a refusal is logged with its status and never raised.

        The alert is already in the JSON-lines log by the time this runs. On 21 September
        2026 twelve digests in one burst drew Telegram's rate limit, and the refusal
        propagated into the engine's cycle as a failed tick. The log line carries the
        status and the error type only, never the request, whose URL holds the token.
        """
        text = _pramana_message(notification.message)
        if len(text) > TELEGRAM_MAX_CHARS:
            cut = TELEGRAM_MAX_CHARS - len(TELEGRAM_TRUNCATION_MARKER)
            text = text[:cut] + TELEGRAM_TRUNCATION_MARKER
        payload = urllib.parse.urlencode({"chat_id": self.chat_id, "text": text}).encode()
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
            data=payload,
            method="POST",
        )
        try:
            response = self.opener(request, timeout=self.timeout_seconds)
        except (urllib.error.URLError, OSError, TimeoutError, ValueError) as error:
            status = getattr(error, "code", None)
            LOGGER.warning(
                "telegram_send_failed code=%s status=%s error=%s",
                notification.code.value,
                status if isinstance(status, int) else "none",
                type(error).__name__,
            )
            return
        close = getattr(response, "close", None)
        if callable(close):
            close()


def _ratio(value: Decimal | None) -> str:
    """An annualised ratio, or a plain statement that the sample could not support one."""
    return "unavailable (too few observations)" if value is None else str(value)


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
            (
                f"Market-return Sharpe: {_ratio(brief.sharpe_ratio)} | "
                f"Sortino: {_ratio(brief.sortino_ratio)} (not strategy performance)"
            ),
            f"XAI: {'; '.join(brief.xai_rationales) or 'none'}",
            f"Trades: {trades}",
            f"Equity: {total_equity} | Realized P&L: {realized_pnl}",
            f"Unrealized P&L: {unrealized_pnl} | Drawdown: {drawdown_fraction}",
        )
    )


def _decision_source(brief: FounderExecutionBrief) -> str:
    """Where the instrument's decision came from, read from the declared rationales."""
    for item in brief.xai_rationales:
        if item.startswith("anthropic_model="):
            return "model"
    for item in brief.xai_rationales:
        if item.startswith("Consensus Skipped: "):
            return f"model skipped ({item[len('Consensus Skipped: '):]})"
    return "deterministic"


def _decision_outcome(brief: FounderExecutionBrief) -> str:
    if brief.paper_order_ids:
        return "ORDER " + ",".join(brief.paper_order_ids)
    if brief.risk_decision == "atlas_non_actionable_proposal":
        return "hold"
    return f"proposal {brief.risk_decision}"


def _needs_detail(brief: FounderExecutionBrief) -> bool:
    """An order, or a directional proposal the risk desk ruled on, earns its full block."""
    return bool(brief.paper_order_ids) or brief.risk_decision != "atlas_non_actionable_proposal"


def _detail_block(brief: FounderExecutionBrief) -> str:
    return "\n".join(
        (
            f"Detail: {brief.subject}",
            f"Swarm: {', '.join(brief.swarm_consensus) or 'none'}",
            f"Risk: {brief.risk_decision} | Stress: {brief.stress_verdict}",
            f"XAI: {'; '.join(brief.xai_rationales) or 'none'}",
        )
    )


def format_cycle_digest(
    briefs: Sequence[FounderExecutionBrief],
    *,
    total_equity: Decimal,
    realized_pnl: Decimal,
    unrealized_pnl: Decimal,
    drawdown_fraction: Decimal,
) -> str:
    """One digest for every instrument the cycle decided on.

    One line per instrument (outcome, decision source, regime), the stance tally, the
    orders, and the book. Instruments that produced an order or a directional proposal
    get their full evidence block, bounded; the rest have their proofs. A cycle over
    twelve names used to send twelve of the single-instrument digests in one burst.
    """
    if not briefs:
        raise ValueError("a cycle digest needs at least one brief")
    stances = Counter(item.mode for item in briefs)
    stance_line = ", ".join(f"{mode} x{count}" for mode, count in stances.most_common())
    orders = [oid for item in briefs for oid in item.paper_order_ids]
    lines = [
        FOUNDER_EXECUTION_BRIEF_HEADER,
        f"Generated: {briefs[0].generated_at.isoformat()}",
        f"Market: {briefs[0].market_state.value} | {len(briefs)} instruments",
        f"Stance: {stance_line}",
        f"Trades: {', '.join(orders) or 'none'}",
        "Decisions:",
    ]
    for item in briefs:
        regime = item.market_regime.split(" (", 1)[0]
        lines.append(f"{item.subject}: {_decision_outcome(item)} | {_decision_source(item)} | {regime}")
    detailed = [item for item in briefs if _needs_detail(item)]
    if not detailed:
        lines.append("Detail: none (no order or directional proposal this cycle)")
    for item in detailed[:MAX_DIGEST_DETAILS]:
        lines.append(_detail_block(item))
    if len(detailed) > MAX_DIGEST_DETAILS:
        lines.append(f"Detail: +{len(detailed) - MAX_DIGEST_DETAILS} more in the proofs")
    lines.extend(
        (
            f"Equity: {total_equity} | Realized P&L: {realized_pnl}",
            f"Unrealized P&L: {unrealized_pnl} | Drawdown: {drawdown_fraction}",
        )
    )
    return "\n".join(lines)


class TradingNotificationDispatcher(BaseTradingNotificationDispatcher):
    def dispatch_cycle_digest(
        self,
        briefs: Sequence[FounderExecutionBrief],
        *,
        total_equity: Decimal,
        realized_pnl: Decimal,
        unrealized_pnl: Decimal,
        drawdown_fraction: Decimal,
        tenant_id: str = "default",
    ) -> TradingNotification:
        message = format_cycle_digest(
            briefs,
            total_equity=total_equity,
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized_pnl,
            drawdown_fraction=drawdown_fraction,
        )
        orders = sum(len(item.paper_order_ids) for item in briefs)
        return self.dispatch(
            TradingAlertCode.CADENCE_BRIEF,
            message,
            tenant_id=tenant_id,
            priority=AlertPriority.INFO,
            metadata={
                "market_state": briefs[0].market_state.value,
                "instruments": str(len(briefs)),
                "paper_orders": str(orders),
                "subjects": ",".join(item.subject for item in briefs),
            },
        )

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
