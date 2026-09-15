"""Alerts must survive the container that raised them."""
from __future__ import annotations

import json
import logging
import stat
from datetime import datetime, timezone

from quant_ai.daemon import _env_notifications
from quant_ai.execution.notifications import TelegramNotificationAdapter
from quant_ai.notifications.trading import (
    AlertPriority,
    JsonlFileSink,
    TradingAlertCode,
    TradingNotificationDispatcher,
)


def read_lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_every_alert_is_one_json_line_in_a_private_file(tmp_path) -> None:
    path = tmp_path / "logs" / "alerts.jsonl"
    dispatcher = TradingNotificationDispatcher((JsonlFileSink(path),))

    dispatcher.dispatch(
        TradingAlertCode.KILL_SWITCH_ENGAGED,
        "Risk kill switch engaged",
        tenant_id="ghost",
        metadata={"reason": "max_drawdown"},
    )
    # A founder brief is multi-line; JSON escaping must keep it on a single line.
    dispatcher.dispatch(
        TradingAlertCode.CADENCE_BRIEF,
        "Generated: now\nEquity: 100000\nDrawdown: 0.01",
        tenant_id="ghost",
        priority=AlertPriority.INFO,
    )

    assert path.parent.is_dir()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    lines = read_lines(path)
    assert len(lines) == 2
    first, second = lines
    assert first["code"] == "KILL_SWITCH_ENGAGED"
    assert first["priority"] == "CRITICAL"
    assert first["message"] == "[PRAMANA] Risk kill switch engaged"
    assert first["tenant_id"] == "ghost"
    assert first["metadata"] == {"reason": "max_drawdown"}
    assert first["notification_id"]
    # The full console payload plus when the line was written down, both UTC ISO-8601.
    for field in ("created_at", "logged_at"):
        assert datetime.fromisoformat(first[field]).tzinfo is not None
    assert datetime.fromisoformat(first["logged_at"]).astimezone(timezone.utc)
    assert second["code"] == "CADENCE_BRIEF"
    assert second["priority"] == "INFO"
    assert "\n" in second["message"]


def test_appends_to_an_existing_log_rather_than_truncating_it(tmp_path) -> None:
    path = tmp_path / "alerts.jsonl"
    sink = JsonlFileSink(path)
    first = TradingNotificationDispatcher((sink,))
    first.dispatch(TradingAlertCode.PROVIDER_STALE, "Provider stale")
    # A restarted container builds a new sink against the same file.
    TradingNotificationDispatcher((JsonlFileSink(path),)).dispatch(
        TradingAlertCode.STOP_LOSS_TRIGGERED, "Stop loss triggered"
    )

    codes = [line["code"] for line in read_lines(path)]
    assert codes == ["PROVIDER_STALE", "STOP_LOSS_TRIGGERED"]


def test_a_write_failure_is_logged_and_never_reaches_the_caller(tmp_path, caplog) -> None:
    # A directory where the log belongs: every write fails, for the life of the process.
    blocked = tmp_path / "alerts.jsonl"
    blocked.mkdir()
    dispatcher = TradingNotificationDispatcher((JsonlFileSink(blocked),))

    with caplog.at_level(logging.ERROR, logger="quant_ai.trading_alerts"):
        alert = dispatcher.dispatch(
            TradingAlertCode.CADENCE_TICK_FAILED, "Cadence tick failed"
        )

    assert alert.code == TradingAlertCode.CADENCE_TICK_FAILED
    assert dispatcher.pending() == (alert,)
    assert "alert_log_write_failed" in caplog.text


def test_a_failing_sink_does_not_stop_the_sinks_after_it(tmp_path) -> None:
    path = tmp_path / "alerts.jsonl"
    broken = tmp_path / "broken.jsonl"
    broken.mkdir()
    dispatcher = TradingNotificationDispatcher((JsonlFileSink(broken), JsonlFileSink(path)))

    dispatcher.dispatch(TradingAlertCode.MAX_DRAWDOWN_BREACHED, "Drawdown breached")

    assert [line["code"] for line in read_lines(path)] == ["MAX_DRAWDOWN_BREACHED"]


def test_env_notifications_is_durable_without_any_telegram_configuration(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("PRAMANA_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("PRAMANA_TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setenv("PRAMANA_ALERT_LOG", str(tmp_path / "alerts.jsonl"))

    dispatcher = _env_notifications()
    dispatcher.dispatch(TradingAlertCode.KILL_SWITCH_ENGAGED, "Kill switch engaged")

    assert isinstance(dispatcher, TradingNotificationDispatcher)
    assert not any(isinstance(sink, TelegramNotificationAdapter) for sink in dispatcher.sinks)
    assert any(isinstance(sink, JsonlFileSink) for sink in dispatcher.sinks)
    lines = read_lines(tmp_path / "alerts.jsonl")
    assert [line["code"] for line in lines] == ["KILL_SWITCH_ENGAGED"]


def test_env_notifications_adds_telegram_when_both_settings_are_present(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("PRAMANA_TELEGRAM_BOT_TOKEN", "token-value")
    monkeypatch.setenv("PRAMANA_TELEGRAM_CHAT_ID", "chat-value")
    monkeypatch.setenv("PRAMANA_ALERT_LOG", str(tmp_path / "alerts.jsonl"))

    sinks = _env_notifications().sinks

    assert sum(isinstance(sink, TelegramNotificationAdapter) for sink in sinks) == 1
    assert sum(isinstance(sink, JsonlFileSink) for sink in sinks) == 1


def test_env_notifications_ignores_a_half_configured_telegram(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PRAMANA_TELEGRAM_BOT_TOKEN", "token-value")
    monkeypatch.setenv("PRAMANA_TELEGRAM_CHAT_ID", "   ")
    monkeypatch.setenv("PRAMANA_ALERT_LOG", str(tmp_path / "alerts.jsonl"))

    sinks = _env_notifications().sinks

    assert not any(isinstance(sink, TelegramNotificationAdapter) for sink in sinks)
    assert any(isinstance(sink, JsonlFileSink) for sink in sinks)


def test_alert_log_defaults_next_to_the_ledger(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("PRAMANA_ALERT_LOG", raising=False)
    monkeypatch.delenv("PRAMANA_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("PRAMANA_TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setenv("PRAMANA_LEDGER_PATH", str(tmp_path / "pramana.db"))

    sink = next(sink for sink in _env_notifications().sinks if isinstance(sink, JsonlFileSink))

    assert sink.path == tmp_path / "alerts.jsonl"


def test_the_outbox_keeps_the_newest_alerts_within_its_cap() -> None:
    dispatcher = TradingNotificationDispatcher((), retained=3)

    for index in range(10):
        dispatcher.dispatch(TradingAlertCode.PROVIDER_STALE, f"stale {index}", tenant_id="ghost")

    pending = dispatcher.pending()
    assert len(pending) == 3
    assert [item.message for item in pending] == [
        "[PRAMANA] stale 7",
        "[PRAMANA] stale 8",
        "[PRAMANA] stale 9",
    ]
    # The tenant filter and the last-element access every caller uses still hold.
    assert dispatcher.pending("ghost") == pending
    assert dispatcher.pending()[-1].message == "[PRAMANA] stale 9"
    assert dispatcher.pending("other") == ()
