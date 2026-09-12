from quant_ai.notifications.trading import (
    AlertPriority,
    TradingAlertCode,
    TradingNotification,
    TradingNotificationDispatcher,
)


class CapturingSink:
    def __init__(self) -> None:
        self.items: list[TradingNotification] = []

    def send(self, notification: TradingNotification) -> None:
        self.items.append(notification)


def test_critical_alerts_enter_outbox_and_route_to_sink() -> None:
    sink = CapturingSink()
    dispatcher = TradingNotificationDispatcher((sink,))
    alert = dispatcher.dispatch(
        TradingAlertCode.MAX_DRAWDOWN_BREACHED,
        "Portfolio drawdown threshold breached",
        tenant_id="tenant-a",
        metadata={"drawdown": "0.10"},
    )
    assert alert.priority == AlertPriority.CRITICAL
    assert dispatcher.pending("tenant-a") == (alert,)
    assert sink.items == [alert]


def test_kill_switch_alert_code_is_supported() -> None:
    sink = CapturingSink()
    dispatcher = TradingNotificationDispatcher((sink,))
    alert = dispatcher.dispatch(
        TradingAlertCode.KILL_SWITCH_ENGAGED,
        "Risk kill switch engaged",
        priority=AlertPriority.CRITICAL,
    )
    assert alert.code == TradingAlertCode.KILL_SWITCH_ENGAGED
