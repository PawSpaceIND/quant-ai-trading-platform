from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass(frozen=True)
class FeedHeartbeat:
    last_update: datetime
    max_age: timedelta = timedelta(seconds=5)

    def is_fresh(self, now: datetime | None = None) -> bool:
        current = now or datetime.now(timezone.utc)
        if self.last_update.tzinfo is None or self.last_update.utcoffset() is None:
            raise ValueError("last_update must be timezone-aware")
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        return current - self.last_update <= self.max_age

    def assert_fresh(self, now: datetime | None = None) -> None:
        if not self.is_fresh(now):
            raise RuntimeError("stale_market_data")
