from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True)
class DecisionCadence:
    atlas_cycle: timedelta = timedelta(minutes=10)
    founder_daily_brief: timedelta = timedelta(days=1)
    founder_weekly_review: timedelta = timedelta(days=7)
    founder_monthly_review: timedelta = timedelta(days=30)
    founder_yearly_review: timedelta = timedelta(days=365)

    def __post_init__(self) -> None:
        if self.atlas_cycle < timedelta(minutes=1):
            raise ValueError("atlas cadence must be at least one minute")
