from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import ClassVar


class DataCategory(str, Enum):
    PRICE = "PRICE"
    NEWS = "NEWS"
    MACRO = "MACRO"
    FUNDAMENTAL = "FUNDAMENTAL"


class FreshnessState(str, Enum):
    FRESH = "FRESH"
    STALE = "STALE"
    MISSING = "MISSING"


@dataclass(frozen=True)
class FreshnessResult:
    state: FreshnessState
    age_seconds: int | None
    ttl_seconds: int
    confidence_multiplier: Decimal


class FreshnessValidator:
    TTL: ClassVar[dict[DataCategory, int]] = {
        DataCategory.PRICE: 60,
        DataCategory.NEWS: 30 * 60,
        DataCategory.MACRO: 4 * 60 * 60,
        DataCategory.FUNDAMENTAL: 24 * 60 * 60,
    }

    def validate(self, category: DataCategory, observed_at: datetime | None, now: datetime) -> FreshnessResult:
        ttl = self.TTL[category]
        if observed_at is None:
            return FreshnessResult(FreshnessState.MISSING, None, ttl, Decimal(0))
        if observed_at.tzinfo is None or now.tzinfo is None:
            raise ValueError("freshness timestamps must be timezone-aware")
        age = max(0, int((now - observed_at).total_seconds()))
        if age <= ttl:
            return FreshnessResult(FreshnessState.FRESH, age, ttl, Decimal(1))
        ratio = Decimal(ttl) / Decimal(max(age, 1))
        penalty = max(Decimal("0.10"), min(Decimal("0.50"), ratio))
        return FreshnessResult(FreshnessState.STALE, age, ttl, penalty)


class IntelligenceDataCache:
    def __init__(self) -> None:
        self._items: dict[str, tuple[object, datetime]] = {}

    def put(self, key: str, value: object, observed_at: datetime) -> None:
        self._items[key] = (value, observed_at)

    def get(self, key: str) -> tuple[object, datetime] | None:
        return self._items.get(key)
