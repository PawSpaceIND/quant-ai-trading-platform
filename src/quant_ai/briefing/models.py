from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum


class BriefPeriod(str, Enum):
    TEN_MINUTE = "TEN_MINUTE"
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"
    YEARLY = "YEARLY"


@dataclass(frozen=True)
class FounderGoals:
    target_return: Decimal
    max_drawdown: Decimal
    max_daily_loss: Decimal
    minimum_cash_reserve: Decimal


@dataclass(frozen=True)
class FounderBrief:
    generated_at: datetime
    period: BriefPeriod
    nav: Decimal
    pnl: Decimal
    drawdown: Decimal
    cash_fraction: Decimal
    goal_status: str
    critical_actions: tuple[str, ...]
    atlas_summary: tuple[str, ...]
    country_recommendations: tuple[str, ...]
    agent_health_score: Decimal
    founder_decisions_required: tuple[str, ...]

    def to_json(self) -> str:
        def normalize(value: object) -> object:
            if isinstance(value, Decimal):
                return str(value)
            if isinstance(value, datetime):
                return value.isoformat()
            if isinstance(value, Enum):
                return value.value
            if isinstance(value, tuple):
                return [normalize(item) for item in value]
            if isinstance(value, dict):
                return {key: normalize(item) for key, item in value.items()}
            return value

        return json.dumps(normalize(asdict(self)), sort_keys=True, separators=(",", ":"))
