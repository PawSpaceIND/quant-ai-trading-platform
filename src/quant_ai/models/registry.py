from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class ModelVersion:
    model_id: str
    version: str
    training_cutoff: datetime
    artifact_digest: str
    approved: bool = False

    def __post_init__(self) -> None:
        if self.training_cutoff.tzinfo is None or self.training_cutoff.utcoffset() is None:
            raise ValueError("training cutoff must be timezone-aware")
        if not self.artifact_digest:
            raise ValueError("artifact digest is required")


class ModelRegistry:
    def __init__(self) -> None:
        self._versions: dict[tuple[str, str], ModelVersion] = {}

    def register(self, version: ModelVersion) -> None:
        key = (version.model_id, version.version)
        if key in self._versions:
            raise ValueError("model version already registered")
        self._versions[key] = version

    def get(self, model_id: str, version: str) -> ModelVersion:
        return self._versions[(model_id, version)]

    def approved_versions(self, model_id: str) -> tuple[ModelVersion, ...]:
        return tuple(item for item in self._versions.values() if item.model_id == model_id and item.approved)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
