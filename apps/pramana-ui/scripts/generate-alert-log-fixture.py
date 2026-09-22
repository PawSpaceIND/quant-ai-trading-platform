"""Write a fixture alert log with the engine's own JsonlFileSink.

Usage: generate-alert-log-fixture.py <target.jsonl> [tenant]

Hand-writing these lines would test the reader against my idea of the format instead of
the writer's, which is exactly how the decision-quality parser drifted.
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, "src")
from quant_ai.notifications.trading import (
    AlertPriority,
    JsonlFileSink,
    TradingAlertCode,
    TradingNotification,
)

BASE = datetime(2026, 9, 21, 3, 45, tzinfo=timezone.utc)
ROWS = [
    (TradingAlertCode.MACRO_PROVIDER_UNAVAILABLE, AlertPriority.CRITICAL,
     "Macro provider refused at boot; every macro-reading specialist will report zero confidence.",
     {"provider": "FRED", "reason": "unauthorized"}),
    (TradingAlertCode.SESSION_PLAN_READY, AlertPriority.INFO,
     "Pre-open session plan written: defensive posture, two names on focus.", {"names": "2"}),
    (TradingAlertCode.CADENCE_TICK_FAILED, AlertPriority.HIGH,
     "Cadence tick aborted before any decision was recorded.", {"stage": "analysis"}),
    (TradingAlertCode.OVERNIGHT_GAP_UNEXPLAINED, AlertPriority.HIGH,
     "Price step across the session boundary that no recorded corporate action explains.",
     {"symbol": "TRENT", "step_pct": "-11.4"}),
    (TradingAlertCode.KILL_SWITCH_ENGAGED, AlertPriority.CRITICAL,
     "Trading halted: portfolio_daily_loss_limit", {"reason": "portfolio_daily_loss_limit"}),
]

def main(target: Path, tenant: str = "ghost") -> None:
    sink = JsonlFileSink(target)
    for index, (code, priority, message, metadata) in enumerate(ROWS):
        sink.send(TradingNotification(
            notification_id=f"{index:032x}", tenant_id=tenant, code=code, priority=priority,
            message=message, created_at=BASE + timedelta(minutes=7 * index), metadata=metadata))
    # Another account's alert, on the same shared volume.
    sink.send(TradingNotification(
        notification_id="f" * 32, tenant_id="other-tenant", code=TradingAlertCode.KILL_SWITCH_ENGAGED,
        priority=AlertPriority.CRITICAL, message="OTHER-TENANT-PRIVATE halt",
        created_at=BASE + timedelta(hours=1), metadata={"reason": "other"}))

    # The sink stamps logged_at with the wall clock, which the schema is right to do and a
    # committed fixture cannot keep. Pin it afterwards so the file is reproducible, leaving
    # every other field exactly as the writer produced it.
    lines = target.read_text(encoding="utf-8").splitlines()
    pinned = []
    for index, line in enumerate(lines):
        row = json.loads(line)
        row["logged_at"] = (BASE + timedelta(minutes=7 * index, seconds=3)).isoformat()
        pinned.append(json.dumps(row, sort_keys=True, separators=(",", ":")))
    target.write_text("\n".join(pinned) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main(Path(sys.argv[1]), *sys.argv[2:3])
