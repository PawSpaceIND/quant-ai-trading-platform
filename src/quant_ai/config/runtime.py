from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RuntimeMode(str, Enum):
    RESEARCH = "RESEARCH"
    PAPER = "PAPER"
    SHADOW = "SHADOW"
    LIVE = "LIVE"


@dataclass(frozen=True)
class RuntimeConfig:
    mode: RuntimeMode = RuntimeMode.PAPER
    allow_live_orders: bool = False
    require_human_approval: bool = True
    max_daily_loss_pct: float = 0.02

    def validate(self) -> None:
        if self.mode == RuntimeMode.LIVE and not self.allow_live_orders:
            raise ValueError("live mode requires explicit allow_live_orders")
        if not 0 < self.max_daily_loss_pct < 1:
            raise ValueError("max_daily_loss_pct must be between 0 and 1")
