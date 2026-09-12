from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass


@dataclass(frozen=True)
class UsageRecord:
    tenant_id: str
    metric: str
    count: int


class UsageMeter:
    def __init__(self) -> None:
        self._counts: dict[tuple[str, str], int] = defaultdict(int)

    def increment(self, tenant_id: str, metric: str, amount: int = 1) -> None:
        if amount <= 0:
            raise ValueError("usage amount must be positive")
        self._counts[(tenant_id, metric)] += amount

    def get(self, tenant_id: str, metric: str) -> int:
        return self._counts[(tenant_id, metric)]

    def records(self, tenant_id: str) -> tuple[UsageRecord, ...]:
        rows = [UsageRecord(t, metric, count) for (t, metric), count in self._counts.items() if t == tenant_id]
        return tuple(sorted(rows, key=lambda row: row.metric))
