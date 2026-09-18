from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReadinessCheck:
    name: str
    ready: bool
    detail: str = ""

    def __post_init__(self) -> None:
        if type(self.name) is not str or not self.name or self.name != self.name.strip():
            raise ValueError("readiness_check_name_invalid")
        if type(self.ready) is not bool:
            raise ValueError("readiness_check_status_must_be_boolean")
        if type(self.detail) is not str:
            raise ValueError("readiness_check_detail_invalid")


@dataclass(frozen=True)
class ReadinessReport:
    checks: tuple[ReadinessCheck, ...]

    def __post_init__(self) -> None:
        if type(self.checks) not in (tuple, list):
            raise ValueError("readiness_check_inventory_invalid")
        checks = tuple(self.checks)
        if any(type(check) is not ReadinessCheck for check in checks):
            raise ValueError("readiness_check_type_invalid")
        if len({check.name for check in checks}) != len(checks):
            raise ValueError("readiness_check_names_must_be_unique")
        # A frozen report must not retain a caller-owned mutable list.
        object.__setattr__(self, "checks", checks)

    @property
    def ready(self) -> bool:
        return bool(self.checks) and all(check.ready is True for check in self.checks)

    @property
    def blockers(self) -> tuple[ReadinessCheck, ...]:
        return tuple(check for check in self.checks if check.ready is not True)
