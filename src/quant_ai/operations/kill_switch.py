from __future__ import annotations

from dataclasses import dataclass


@dataclass
class KillSwitch:
    engaged: bool = False
    reason: str | None = None

    def engage(self, reason: str) -> None:
        if not reason.strip():
            raise ValueError("kill switch reason is required")
        self.engaged = True
        self.reason = reason

    def reset(self) -> None:
        self.engaged = False
        self.reason = None

    def assert_trading_allowed(self) -> None:
        if self.engaged:
            raise RuntimeError(f"trading disabled: {self.reason}")
