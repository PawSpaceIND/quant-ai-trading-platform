from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from broker_observation_fixture import fixture

from quant_ai.execution.broker_observation import inspect_capture


@pytest.mark.parametrize(
    "clock",
    [
        "2026-09-14T18:30:00+00:00",  # Exact IST midnight.
        "2026-09-14T18:30:01+00:00",
        "2026-09-14T18:35:00+00:00",
        "2026-09-15T06:00:00+00:00",
    ],
)
def test_synthetic_daily_book_never_uses_previous_day_or_future_executions(clock):
    now = datetime.fromisoformat(clock)
    capture = fixture(now)
    assert inspect_capture(capture) == {
        "status": "consistent",
        "issueCount": 0,
        "issues": [],
        "orderCount": 14,
        "tradeCount": 26,
        "openOrderCount": 1,
    }
    for group in ("orders", "ordersBefore", "trades", "tradesBefore"):
        for row in capture[group]:
            at = datetime.fromisoformat(row["at"])
            assert at <= now
            assert (
                at.astimezone(ZoneInfo("Asia/Kolkata")).date()
                == now.astimezone(ZoneInfo("Asia/Kolkata")).date()
            )
