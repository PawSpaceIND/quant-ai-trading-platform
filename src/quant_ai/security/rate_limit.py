from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone


class SlidingWindowRateLimiter:
    def __init__(self, limit: int, window: timedelta) -> None:
        if limit <= 0 or window.total_seconds() <= 0:
            raise ValueError("invalid rate limit")
        self.limit = limit
        self.window = window
        self._events: dict[str, deque[datetime]] = defaultdict(deque)

    def allow(self, key: str, now: datetime | None = None) -> bool:
        current = now or datetime.now(timezone.utc)
        events = self._events[key]
        cutoff = current - self.window
        while events and events[0] <= cutoff:
            events.popleft()
        if len(events) >= self.limit:
            return False
        events.append(current)
        return True
