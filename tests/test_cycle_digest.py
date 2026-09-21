"""One Telegram digest per cycle, and no delivery failure ever reaches the cadence.

On 21 September 2026 the pilot's first twelve-name session sent one digest per instrument
every ten minutes, twelve in one burst. Telegram accepted about three and refused the rest,
and the refusal raised out of the cycle that was reporting the decisions, which the
supervisor counts as a failed tick. These tests pin the replacement: one digest for the
whole cycle, a sender that logs a refusal and returns, and a dispatcher that carries on
past any sink that raises.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from quant_ai.execution.briefing import FOUNDER_EXECUTION_BRIEF_HEADER, FounderExecutionBrief
from quant_ai.execution.notifications import (
    TELEGRAM_MAX_CHARS,
    TELEGRAM_TRUNCATION_MARKER,
    TelegramNotificationAdapter,
    TradingNotificationDispatcher,
    format_cycle_digest,
)
from quant_ai.execution.session import MarketState
from quant_ai.notifications.trading import AlertPriority, TradingAlertCode

NOW = datetime(2026, 9, 21, 6, 0, tzinfo=timezone.utc)
BOOK = {"total_equity": Decimal(100000), "realized_pnl": Decimal(0),
        "unrealized_pnl": Decimal(0), "drawdown_fraction": Decimal(0)}
TOKEN = "123456:SECRET_BOT_TOKEN_NEVER_LOGGED"


def brief(subject: str, *, rationales=("anthropic_model=claude-sonnet-5", "xai_summary=hold"),
          orders=(), risk="atlas_non_actionable_proposal", mode="PRESERVE_CAPITAL") -> FounderExecutionBrief:
    return FounderExecutionBrief(
        NOW, MarketState.REGULAR_HOURS, subject, mode,
        ("technical-quant-mas:NEUTRAL:0.30", "risk-desk:NEUTRAL:0.70"), risk, tuple(orders),
        ("price=FRESH",), "ranging@1d (exposure:MEAN_REVERTING)", "PASS",
        xai_rationales=tuple(rationales),
    )


class CaptureSink:
    def __init__(self) -> None:
        self.items = []

    def send(self, notification) -> None:
        self.items.append(notification)


class RaisingSink:
    def send(self, notification) -> None:
        raise RuntimeError("sink down")


class Opener:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        if self.error is not None:
            raise self.error


def test_the_cycle_digest_names_every_instrument_once_with_its_outcome_and_source():
    briefs = [
        brief("TRENT"),
        brief("BAJAJ-AUTO", rationales=("Consensus Skipped: API Timeout",)),
        brief("INDIGO", rationales=("Consensus Skipped: Invalid Schema",)),
        brief("GOLDBEES", rationales=("regime=ranging", "stress=True:NO_ACTION")),
    ]
    text = format_cycle_digest(briefs, **BOOK)
    lines = text.split("\n")
    assert lines[0] == FOUNDER_EXECUTION_BRIEF_HEADER
    assert lines[1] == f"Generated: {NOW.isoformat()}"
    assert lines[2] == "Market: REGULAR_HOURS | 4 instruments"
    assert lines[3] == "Stance: PRESERVE_CAPITAL x4"
    assert lines[4] == "Trades: none"
    assert "TRENT: hold | model | ranging@1d" in lines
    assert "BAJAJ-AUTO: hold | model skipped (API Timeout) | ranging@1d" in lines
    assert "INDIGO: hold | model skipped (Invalid Schema) | ranging@1d" in lines
    assert "GOLDBEES: hold | deterministic | ranging@1d" in lines
    assert "Detail: none (no order or directional proposal this cycle)" in lines
    assert lines[-2] == "Equity: 100000 | Realized P&L: 0"
    assert lines[-1] == "Unrealized P&L: 0 | Drawdown: 0"
    assert text.count("TRENT") == 1
    assert len(text) < TELEGRAM_MAX_CHARS


def test_an_order_or_a_directional_proposal_earns_its_full_evidence_block():
    traded = brief("BEL", mode="PAPER_TRADE", orders=("PAPER-0001",), risk="approved",
                   rationales=("anthropic_model=claude-sonnet-5", "xai_summary=trend confirmed"))
    refused = brief("NTPC", risk="risk_rejected_sector_concentration")
    text = format_cycle_digest([brief("TRENT"), traded, refused], **BOOK)
    assert "Trades: PAPER-0001" in text
    assert "Stance: PRESERVE_CAPITAL x2, PAPER_TRADE x1" in text
    assert "BEL: ORDER PAPER-0001 | model | ranging@1d" in text
    assert "NTPC: proposal risk_rejected_sector_concentration | model | ranging@1d" in text
    assert "Detail: BEL\nSwarm: technical-quant-mas:NEUTRAL:0.30, risk-desk:NEUTRAL:0.70\n" in text
    assert "Risk: approved | Stress: PASS\nXAI: anthropic_model=claude-sonnet-5; xai_summary=trend confirmed" in text
    assert "Detail: NTPC" in text
    assert "Detail: none" not in text


def test_detail_blocks_are_bounded_and_the_rest_point_at_the_proofs():
    briefs = [brief(f"SYM{i}", risk="approved", orders=(f"PAPER-{i:04d}",), mode="PAPER_TRADE") for i in range(5)]
    text = format_cycle_digest(briefs, **BOOK)
    assert text.count("Detail: SYM") == 3
    assert "Detail: +2 more in the proofs" in text


def test_an_empty_cycle_is_refused():
    with pytest.raises(ValueError, match="at least one brief"):
        format_cycle_digest([], **BOOK)


def test_the_dispatcher_sends_one_cadence_brief_for_the_whole_cycle():
    sink = CaptureSink()
    dispatcher = TradingNotificationDispatcher((sink,))
    briefs = [brief("TRENT"), brief("BEL"), brief("SILVERBEES")]
    notification = dispatcher.dispatch_cycle_digest(briefs, tenant_id="ghost", **BOOK)
    assert [item.code for item in sink.items] == [TradingAlertCode.CADENCE_BRIEF]
    assert notification.priority is AlertPriority.INFO
    assert notification.metadata == {
        "market_state": "REGULAR_HOURS", "instruments": "3", "paper_orders": "0",
        "subjects": "TRENT,BEL,SILVERBEES",
    }
    assert notification.message.startswith("[PRAMANA] " + FOUNDER_EXECUTION_BRIEF_HEADER)
    assert "Drawdown" in notification.message


def test_a_raising_sink_is_logged_and_the_sinks_after_it_still_deliver(caplog):
    after = CaptureSink()
    dispatcher = TradingNotificationDispatcher((RaisingSink(), after))
    with caplog.at_level(logging.ERROR, logger="quant_ai.trading_alerts"):
        notification = dispatcher.dispatch(TradingAlertCode.KILL_SWITCH_ENGAGED, "Trading halted: test")
    assert after.items == [notification]
    assert "alert_sink_failed sink=RaisingSink code=KILL_SWITCH_ENGAGED" in caplog.text


@pytest.mark.parametrize("error, status", [
    (urllib.error.HTTPError("https://api.telegram.org/x", 429, "Too Many Requests", {}, None), "429"),
    (urllib.error.URLError("temporary failure in name resolution"), "none"),
    (TimeoutError("timed out"), "none"),
])
def test_a_telegram_refusal_is_logged_with_its_status_and_never_raised(error, status, caplog):
    opener = Opener(error)
    adapter = TelegramNotificationAdapter(TOKEN, "42", opener=opener)
    dispatcher = TradingNotificationDispatcher((adapter,))
    with caplog.at_level(logging.WARNING, logger="quant_ai.trading_alerts"):
        dispatcher.dispatch_cycle_digest([brief("TRENT")], **BOOK)
    assert len(opener.requests) == 1
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("telegram_send_failed"))
    assert line == f"telegram_send_failed code=CADENCE_BRIEF status={status} error={type(error).__name__}"
    assert TOKEN not in caplog.text
    assert "SECRET" not in caplog.text


def test_a_delivered_message_is_posted_once_and_a_long_one_is_cut_below_the_telegram_limit():
    opener = Opener()
    adapter = TelegramNotificationAdapter(TOKEN, "42", opener=opener)
    dispatcher = TradingNotificationDispatcher((adapter,))
    dispatcher.dispatch(TradingAlertCode.CADENCE_BRIEF, "short digest", priority=AlertPriority.INFO)
    dispatcher.dispatch(TradingAlertCode.CADENCE_BRIEF, "x" * 6000, priority=AlertPriority.INFO)
    assert len(opener.requests) == 2
    short = urllib.parse.parse_qs(opener.requests[0].data.decode())
    assert short["chat_id"] == ["42"] and short["text"] == ["[PRAMANA] short digest"]
    long = urllib.parse.parse_qs(opener.requests[1].data.decode())["text"][0]
    assert len(long) == TELEGRAM_MAX_CHARS
    assert long.endswith(TELEGRAM_TRUNCATION_MARKER)
    assert opener.requests[1].full_url.endswith("/sendMessage")
