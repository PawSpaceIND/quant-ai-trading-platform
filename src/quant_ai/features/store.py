"""Append-only point-in-time numeric feature store for decisions and training.

`observed_at` says when the underlying fact belongs to; `available_at` says when the trading
system could first have known this exact revision. Snapshots filter on both, which is what
prevents a later fundamental revision or derived feature from leaking into an earlier model
training/decision row.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Self

_ID = re.compile(r"[A-Za-z0-9._:/-]{1,180}")


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label}_must_be_timezone_aware")
    return value.astimezone(timezone.utc)


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class FeatureObservation:
    observation_id: str
    subject: str
    feature: str
    value: Decimal
    observed_at: datetime
    available_at: datetime
    source_id: str
    schema_id: str

    def __post_init__(self) -> None:
        for value in (self.observation_id, self.subject, self.feature, self.source_id, self.schema_id):
            if not _ID.fullmatch(value):
                raise ValueError("feature_observation_identity_invalid")
        if not self.value.is_finite():
            raise ValueError("feature_observation_value_must_be_finite")
        observed = _aware(self.observed_at, "feature_observed_at")
        available = _aware(self.available_at, "feature_available_at")
        if available < observed:
            # Derived/provider revisions may become available after their observation time,
            # never before the fact they describe occurred.
            raise ValueError("feature_available_before_observed")

    def payload(self) -> dict[str, str]:
        return {
            "observationId": self.observation_id,
            "subject": self.subject,
            "feature": self.feature,
            "value": str(self.value),
            "observedAt": _aware(self.observed_at, "feature_observed_at").isoformat(),
            "availableAt": _aware(self.available_at, "feature_available_at").isoformat(),
            "sourceId": self.source_id,
            "schemaId": self.schema_id,
        }


@dataclass(frozen=True)
class FeatureRequirement:
    feature: str
    max_age_seconds: int
    allowed_sources: frozenset[str] = frozenset()
    required_schema_id: str | None = None

    def __post_init__(self) -> None:
        if not _ID.fullmatch(self.feature) or self.max_age_seconds <= 0:
            raise ValueError("feature_requirement_identity_or_age_invalid")
        if any(not _ID.fullmatch(item) for item in self.allowed_sources):
            raise ValueError("feature_requirement_source_invalid")
        if self.required_schema_id is not None and not _ID.fullmatch(self.required_schema_id):
            raise ValueError("feature_requirement_schema_invalid")


@dataclass(frozen=True)
class FeaturePoint:
    feature: str
    value: Decimal
    observed_at: datetime
    available_at: datetime
    source_id: str
    schema_id: str
    observation_id: str


@dataclass(frozen=True)
class FeatureSnapshot:
    subject: str
    as_of: datetime
    points: tuple[FeaturePoint, ...]
    sha256: str

    def values(self) -> dict[str, Decimal]:
        return {item.feature: item.value for item in self.points}


class PointInTimeFeatureStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            if self.path.exists() and self.path.is_symlink():
                raise ValueError("feature_store_symlink_unsupported")
            if not self.path.exists():
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
        self.db = sqlite3.connect(str(self.path), timeout=10, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        with self.db:
            self.db.execute("""CREATE TABLE IF NOT EXISTS feature_observations(
                observation_id TEXT PRIMARY KEY, subject TEXT NOT NULL, feature TEXT NOT NULL,
                value TEXT NOT NULL, observed_at TEXT NOT NULL, available_at TEXT NOT NULL,
                source_id TEXT NOT NULL, schema_id TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
                UNIQUE(subject,feature,observed_at,available_at,source_id,schema_id)
            )""")
            self.db.execute("CREATE INDEX IF NOT EXISTS feature_snapshot_idx ON feature_observations(subject,feature,available_at,observed_at)")
            for verb in ("UPDATE", "DELETE"):
                self.db.execute(
                    f"CREATE TRIGGER IF NOT EXISTS feature_observations_{verb.lower()}_blocked "
                    f"BEFORE {verb} ON feature_observations BEGIN "
                    "SELECT RAISE(ABORT,'Feature history is append-only'); END"
                )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def close(self) -> None:
        self.db.close()

    def append(self, observation: FeatureObservation) -> None:
        payload = observation.payload()
        digest = _sha(payload)
        with self.db:
            existing = self.db.execute(
                "SELECT payload_sha256 FROM feature_observations WHERE observation_id=?",
                (observation.observation_id,),
            ).fetchone()
            if existing is not None:
                if existing[0] != digest:
                    raise ValueError("feature_observation_id_payload_mismatch")
                return
            try:
                self.db.execute(
                    "INSERT INTO feature_observations VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        observation.observation_id,
                        observation.subject,
                        observation.feature,
                        str(observation.value),
                        payload["observedAt"],
                        payload["availableAt"],
                        observation.source_id,
                        observation.schema_id,
                        digest,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError("duplicate_feature_revision_identity") from error

    def snapshot(
        self,
        *,
        subject: str,
        as_of: datetime,
        requirements: tuple[FeatureRequirement, ...],
    ) -> FeatureSnapshot:
        if not _ID.fullmatch(subject):
            raise ValueError("feature_snapshot_subject_invalid")
        instant = _aware(as_of, "feature_snapshot_as_of")
        if not requirements:
            raise ValueError("feature_snapshot_requirements_required")
        names = [item.feature for item in requirements]
        if len(names) != len(set(names)):
            raise ValueError("duplicate_feature_snapshot_requirement")
        points: list[FeaturePoint] = []
        for requirement in requirements:
            rows = self.db.execute(
                """SELECT * FROM feature_observations
                   WHERE subject=? AND feature=? AND available_at<=? AND observed_at<=?
                   ORDER BY observed_at DESC,available_at DESC,observation_id DESC""",
                (subject, requirement.feature, instant.isoformat(), instant.isoformat()),
            ).fetchall()
            selected = None
            for row in rows:
                if requirement.allowed_sources and row["source_id"] not in requirement.allowed_sources:
                    continue
                if (
                    requirement.required_schema_id is not None
                    and row["schema_id"] != requirement.required_schema_id
                ):
                    continue
                self._verify_row(row)
                selected = row
                break
            if selected is None:
                raise ValueError(f"feature_unavailable:{subject}:{requirement.feature}")
            observed = datetime.fromisoformat(selected["observed_at"])
            age = (instant - observed).total_seconds()
            if age < 0:
                raise ValueError("feature_from_future")
            if age > requirement.max_age_seconds:
                raise ValueError(
                    f"feature_stale:{subject}:{requirement.feature}:{int(age)}"
                )
            points.append(self._point(selected))
        points_tuple = tuple(sorted(points, key=lambda item: item.feature))
        payload = {
            "subject": subject,
            "asOf": instant.isoformat(),
            "points": [
                {
                    "feature": item.feature,
                    "value": str(item.value),
                    "observedAt": item.observed_at.isoformat(),
                    "availableAt": item.available_at.isoformat(),
                    "sourceId": item.source_id,
                    "schemaId": item.schema_id,
                    "observationId": item.observation_id,
                }
                for item in points_tuple
            ],
        }
        return FeatureSnapshot(subject, instant, points_tuple, _sha(payload))

    def materialize(
        self,
        *,
        subject: str,
        decision_times: tuple[datetime, ...],
        requirements: tuple[FeatureRequirement, ...],
    ) -> tuple[FeatureSnapshot, ...]:
        if any(a >= b for a, b in zip(decision_times, decision_times[1:])):
            raise ValueError("feature_materialization_times_must_be_strictly_ordered")
        return tuple(
            self.snapshot(subject=subject, as_of=instant, requirements=requirements)
            for instant in decision_times
        )

    @staticmethod
    def _point(row: sqlite3.Row) -> FeaturePoint:
        try:
            value = Decimal(row["value"])
        except (InvalidOperation, ValueError) as error:
            raise ValueError("feature_store_invalid_decimal") from error
        if not value.is_finite():
            raise ValueError("feature_store_invalid_decimal")
        return FeaturePoint(
            row["feature"], value, datetime.fromisoformat(row["observed_at"]),
            datetime.fromisoformat(row["available_at"]), row["source_id"],
            row["schema_id"], row["observation_id"],
        )

    @staticmethod
    def _verify_row(row: sqlite3.Row) -> None:
        payload = {
            "observationId": row["observation_id"],
            "subject": row["subject"],
            "feature": row["feature"],
            "value": row["value"],
            "observedAt": row["observed_at"],
            "availableAt": row["available_at"],
            "sourceId": row["source_id"],
            "schemaId": row["schema_id"],
        }
        if row["payload_sha256"] != _sha(payload):
            raise ValueError("feature_observation_hash_mismatch")
