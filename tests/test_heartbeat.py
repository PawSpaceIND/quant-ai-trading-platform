from datetime import datetime, timedelta, timezone

import pytest

from quant_ai.marketdata.heartbeat import FeedHeartbeat


def test_fresh_and_stale_feed() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert FeedHeartbeat(now - timedelta(seconds=2)).is_fresh(now)
    with pytest.raises(RuntimeError, match="stale_market_data"):
        FeedHeartbeat(now - timedelta(seconds=10)).assert_fresh(now)
