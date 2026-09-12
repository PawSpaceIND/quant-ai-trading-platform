from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReadinessCheck:
    name: str
    ready: bool
    detail: str = ""


@dataclass(frozen=True)
class ReadinessReport:
    checks: tuple[ReadinessCheck, ...]

    @property
    def ready(self) -> bool:
        return bool(self.checks) and all(check.ready for check in self.checks)

    @property
    def blockers(self) -> tuple[ReadinessCheck, ...]:
        return tuple(check for check in self.checks if not check.ready)
