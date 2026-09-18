"""Append-only probability forecasts resolved against realised after-cost outcomes."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Self

_ID = re.compile(r"[A-Za-z0-9._:/-]{1,180}")
_SHA = re.compile(r"[0-9a-f]{64}")
_EPSILON = Decimal("1e-12")


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label}_must_be_timezone_aware")
    return value.astimezone(timezone.utc)


def _digest_payload(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class ProbabilityForecast:
    forecast_id: str
    pair_id: str
    candidate_id: str
    subject: str
    probability_positive_after_cost: Decimal
    decision_at: datetime
    resolve_after: datetime
    feature_snapshot_sha256: str
    model_artifact_sha256: str
    cost_policy_sha256: str
    regime: str = "UNSPECIFIED"

    def __post_init__(self) -> None:
        for value in (
            self.forecast_id, self.pair_id, self.candidate_id, self.subject, self.regime
        ):
            if not _ID.fullmatch(value):
                raise ValueError("forecast_identity_invalid")
        if (
            not self.probability_positive_after_cost.is_finite()
            or not Decimal(0) <= self.probability_positive_after_cost <= Decimal(1)
        ):
            raise ValueError("forecast_probability_must_be_in_0_1")
        decision = _aware(self.decision_at, "forecast_decision_at")
        resolve = _aware(self.resolve_after, "forecast_resolve_after")
        if resolve <= decision:
            raise ValueError("forecast_resolution_horizon_must_follow_decision")
        for digest in (
            self.feature_snapshot_sha256, self.model_artifact_sha256,
            self.cost_policy_sha256,
        ):
            if not _SHA.fullmatch(digest):
                raise ValueError("forecast_evidence_digest_invalid")


@dataclass(frozen=True)
class ResolvedOutcome:
    forecast_id: str
    resolved_at: datetime
    gross_return: Decimal
    cost_return: Decimal
    after_cost_return: Decimal
    positive_after_cost: bool


@dataclass(frozen=True)
class CalibrationBin:
    lower: Decimal
    upper: Decimal
    count: int
    mean_probability: Decimal
    positive_rate: Decimal


@dataclass(frozen=True)
class ForecastPerformance:
    candidate_id: str
    samples: int
    brier_score: Decimal
    log_loss: Decimal
    expected_calibration_error: Decimal
    mean_probability: Decimal
    positive_rate: Decimal
    mean_after_cost_return: Decimal
    total_after_cost_return: Decimal
    total_cost_return: Decimal
    bins: tuple[CalibrationBin, ...]
    by_regime: tuple[tuple[str, int, Decimal, Decimal], ...]


@dataclass(frozen=True)
class PairedProbabilitySkill:
    candidate_id: str
    baseline_id: str
    pairs: int
    candidate_brier: Decimal
    baseline_brier: Decimal
    brier_improvement: Decimal


class ForecastOutcomeJournal:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            if self.path.exists() and self.path.is_symlink():
                raise ValueError("forecast_journal_symlink_unsupported")
            if not self.path.exists():
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
        self.db = sqlite3.connect(str(self.path), timeout=10, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        with self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS probability_forecasts(
                    forecast_id TEXT PRIMARY KEY, pair_id TEXT NOT NULL,
                    candidate_id TEXT NOT NULL, subject TEXT NOT NULL,
                    probability TEXT NOT NULL, decision_at TEXT NOT NULL,
                    resolve_after TEXT NOT NULL, feature_snapshot_sha256 TEXT NOT NULL,
                    model_artifact_sha256 TEXT NOT NULL, cost_policy_sha256 TEXT NOT NULL,
                    regime TEXT NOT NULL, payload_sha256 TEXT NOT NULL UNIQUE,
                    UNIQUE(pair_id,candidate_id)
                );
                CREATE TABLE IF NOT EXISTS forecast_outcomes(
                    forecast_id TEXT PRIMARY KEY, resolved_at TEXT NOT NULL,
                    gross_return TEXT NOT NULL, cost_return TEXT NOT NULL,
                    after_cost_return TEXT NOT NULL, positive_after_cost INTEGER NOT NULL,
                    payload_sha256 TEXT NOT NULL UNIQUE
                );
            """)
            for table in ("probability_forecasts", "forecast_outcomes"):
                for verb in ("UPDATE", "DELETE"):
                    self.db.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {table}_{verb.lower()}_blocked "
                        f"BEFORE {verb} ON {table} BEGIN "
                        "SELECT RAISE(ABORT,'Forecast history is append-only'); END"
                    )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def close(self) -> None:
        self.db.close()

    @staticmethod
    def _forecast_payload(item: ProbabilityForecast) -> dict[str, object]:
        return {
            "forecastId": item.forecast_id,
            "pairId": item.pair_id,
            "candidateId": item.candidate_id,
            "subject": item.subject,
            "probability": str(item.probability_positive_after_cost),
            "decisionAt": _aware(item.decision_at, "forecast_decision_at").isoformat(),
            "resolveAfter": _aware(item.resolve_after, "forecast_resolve_after").isoformat(),
            "featureSnapshotSha256": item.feature_snapshot_sha256,
            "modelArtifactSha256": item.model_artifact_sha256,
            "costPolicySha256": item.cost_policy_sha256,
            "regime": item.regime,
        }

    def record_forecast(self, item: ProbabilityForecast) -> None:
        with self.db:
            self._insert_forecast(item)

    def _insert_forecast(self, item: ProbabilityForecast) -> None:
        payload = self._forecast_payload(item)
        digest = _digest_payload(payload)
        existing = self.db.execute(
            "SELECT payload_sha256 FROM probability_forecasts WHERE forecast_id=?",
            (item.forecast_id,),
        ).fetchone()
        if existing is not None:
            if existing[0] != digest:
                raise ValueError("forecast_id_payload_mismatch")
            return
        try:
            self.db.execute(
                "INSERT INTO probability_forecasts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    item.forecast_id, item.pair_id, item.candidate_id, item.subject,
                    str(item.probability_positive_after_cost),
                    payload["decisionAt"], payload["resolveAfter"],
                    item.feature_snapshot_sha256, item.model_artifact_sha256,
                    item.cost_policy_sha256, item.regime, digest,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise ValueError("duplicate_candidate_pair_forecast") from error

    def resolve(
        self,
        forecast_id: str,
        *,
        gross_return: Decimal,
        cost_return: Decimal,
        resolved_at: datetime,
    ) -> ResolvedOutcome:
        row = self.db.execute(
            "SELECT * FROM probability_forecasts WHERE forecast_id=?", (forecast_id,)
        ).fetchone()
        if row is None:
            raise KeyError(forecast_id)
        instant = _aware(resolved_at, "forecast_resolved_at")
        if instant < datetime.fromisoformat(row["resolve_after"]):
            raise ValueError("forecast_resolved_before_horizon")
        if (
            not gross_return.is_finite() or not cost_return.is_finite()
            or cost_return < 0
        ):
            raise ValueError("forecast_outcome_returns_invalid")
        after_cost = gross_return - cost_return
        payload = {
            "forecastId": forecast_id,
            "resolvedAt": instant.isoformat(),
            "grossReturn": str(gross_return),
            "costReturn": str(cost_return),
            "afterCostReturn": str(after_cost),
            "positiveAfterCost": after_cost > 0,
        }
        digest = _digest_payload(payload)
        with self.db:
            existing = self.db.execute(
                "SELECT * FROM forecast_outcomes WHERE forecast_id=?", (forecast_id,)
            ).fetchone()
            if existing is not None:
                if existing["payload_sha256"] != digest:
                    raise ValueError("forecast_outcome_payload_mismatch")
                return self._outcome(existing)
            self.db.execute(
                "INSERT INTO forecast_outcomes VALUES(?,?,?,?,?,?,?)",
                (
                    forecast_id, instant.isoformat(), str(gross_return), str(cost_return),
                    str(after_cost), int(after_cost > 0), digest,
                ),
            )
        return ResolvedOutcome(
            forecast_id, instant, gross_return, cost_return, after_cost, after_cost > 0
        )

    def performance(self, candidate_id: str, *, min_samples: int = 30) -> ForecastPerformance:
        rows = self._resolved_rows(candidate_id)
        if len(rows) < min_samples:
            raise ValueError(f"forecast_performance_sample_too_small:{len(rows)}:{min_samples}")
        probabilities = [Decimal(row["probability"]) for row in rows]
        targets = [Decimal(row["positive_after_cost"]) for row in rows]
        after_cost = [Decimal(row["after_cost_return"]) for row in rows]
        costs = [Decimal(row["cost_return"]) for row in rows]
        n = Decimal(len(rows))
        brier = sum(((p - y) ** 2 for p, y in zip(probabilities, targets)), Decimal(0)) / n
        log_loss = sum(
            (
                -(y * self._ln_probability(p) + (Decimal(1) - y) * self._ln_probability(Decimal(1) - p))
                for p, y in zip(probabilities, targets)
            ),
            Decimal(0),
        ) / n
        bins = self._calibration_bins(probabilities, targets)
        ece = sum(
            (
                Decimal(item.count) / n * abs(item.mean_probability - item.positive_rate)
                for item in bins
            ),
            Decimal(0),
        )
        regimes: list[tuple[str, int, Decimal, Decimal]] = []
        for regime in sorted({row["regime"] for row in rows}):
            subset = [row for row in rows if row["regime"] == regime]
            subset_n = Decimal(len(subset))
            subset_brier = sum(
                (
                    (Decimal(row["probability"]) - Decimal(row["positive_after_cost"])) ** 2
                    for row in subset
                ),
                Decimal(0),
            ) / subset_n
            subset_return = sum((Decimal(row["after_cost_return"]) for row in subset), Decimal(0)) / subset_n
            regimes.append((regime, len(subset), subset_brier, subset_return))
        return ForecastPerformance(
            candidate_id=candidate_id,
            samples=len(rows),
            brier_score=brier,
            log_loss=log_loss,
            expected_calibration_error=ece,
            mean_probability=sum(probabilities, Decimal(0)) / n,
            positive_rate=sum(targets, Decimal(0)) / n,
            mean_after_cost_return=sum(after_cost, Decimal(0)) / n,
            total_after_cost_return=sum(after_cost, Decimal(0)),
            total_cost_return=sum(costs, Decimal(0)),
            bins=bins,
            by_regime=tuple(regimes),
        )

    def paired_skill(
        self, candidate_id: str, baseline_id: str, *, min_pairs: int = 30
    ) -> PairedProbabilitySkill:
        candidate = {row["pair_id"]: row for row in self._resolved_rows(candidate_id)}
        baseline = {row["pair_id"]: row for row in self._resolved_rows(baseline_id)}
        pair_ids = sorted(set(candidate) & set(baseline))
        if len(pair_ids) < min_pairs:
            raise ValueError(f"paired_probability_sample_too_small:{len(pair_ids)}:{min_pairs}")
        candidate_errors: list[Decimal] = []
        baseline_errors: list[Decimal] = []
        for pair_id in pair_ids:
            left, right = candidate[pair_id], baseline[pair_id]
            # Paired forecasts must be judged on the same realised outcome. If their
            # resolved returns differ, comparison is invalid rather than averaged away.
            if (
                left["after_cost_return"] != right["after_cost_return"]
                or left["positive_after_cost"] != right["positive_after_cost"]
            ):
                raise ValueError(f"paired_forecast_outcome_mismatch:{pair_id}")
            target = Decimal(left["positive_after_cost"])
            candidate_errors.append((Decimal(left["probability"]) - target) ** 2)
            baseline_errors.append((Decimal(right["probability"]) - target) ** 2)
        n = Decimal(len(pair_ids))
        candidate_brier = sum(candidate_errors, Decimal(0)) / n
        baseline_brier = sum(baseline_errors, Decimal(0)) / n
        return PairedProbabilitySkill(
            candidate_id, baseline_id, len(pair_ids), candidate_brier, baseline_brier,
            baseline_brier - candidate_brier,
        )

    def _resolved_rows(self, candidate_id: str) -> list[sqlite3.Row]:
        return self.db.execute(
            """SELECT f.*,o.resolved_at,o.gross_return,o.cost_return,o.after_cost_return,
                      o.positive_after_cost,o.payload_sha256 AS outcome_sha256
               FROM probability_forecasts f JOIN forecast_outcomes o USING(forecast_id)
               WHERE f.candidate_id=? ORDER BY f.decision_at,f.forecast_id""",
            (candidate_id,),
        ).fetchall()

    @staticmethod
    def _ln_probability(value: Decimal) -> Decimal:
        bounded = max(_EPSILON, min(Decimal(1) - _EPSILON, value))
        return bounded.ln()

    @staticmethod
    def _calibration_bins(
        probabilities: Sequence[Decimal], targets: Sequence[Decimal]
    ) -> tuple[CalibrationBin, ...]:
        buckets: list[list[tuple[Decimal, Decimal]]] = [[] for _ in range(10)]
        for probability, target in zip(probabilities, targets):
            index = min(
                9,
                int((probability * Decimal(10)).to_integral_value(rounding=ROUND_FLOOR)),
            )
            buckets[index].append((probability, target))
        result = []
        for index, rows in enumerate(buckets):
            if not rows:
                continue
            count = Decimal(len(rows))
            result.append(
                CalibrationBin(
                    Decimal(index) / Decimal(10),
                    Decimal(index + 1) / Decimal(10),
                    len(rows),
                    sum((row[0] for row in rows), Decimal(0)) / count,
                    sum((row[1] for row in rows), Decimal(0)) / count,
                )
            )
        return tuple(result)

    @staticmethod
    def _outcome(row: sqlite3.Row) -> ResolvedOutcome:
        return ResolvedOutcome(
            row["forecast_id"], datetime.fromisoformat(row["resolved_at"]),
            Decimal(row["gross_return"]), Decimal(row["cost_return"]),
            Decimal(row["after_cost_return"]), bool(row["positive_after_cost"]),
        )


def candidate_evaluation_from_outcomes(
    journal: ForecastOutcomeJournal,
    *,
    candidate_id: str,
    baseline_id: str,
    max_drawdown: Decimal,
    holdout_sha256: str,
    forward_paper_sha256: str,
    calibration_sha256: str,
    execution_stress_sha256: str,
    trial_register_sha256: str,
    min_samples: int = 30,
):
    """Build the promotion contract from immutable paired after-cost forecast evidence."""
    from quant_ai.learning.contracts import CandidateEvaluation

    performance = journal.performance(candidate_id, min_samples=min_samples)
    skill = journal.paired_skill(candidate_id, baseline_id, min_pairs=min_samples)
    if performance.samples != skill.pairs:
        raise ValueError(
            f"candidate_evaluation_requires_all_outcomes_paired:{performance.samples}:{skill.pairs}"
        )
    return CandidateEvaluation(
        candidate_id=candidate_id,
        resolved_probability_count=skill.pairs,
        brier_score=skill.candidate_brier,
        baseline_brier_score=skill.baseline_brier,
        after_cost_expectancy=performance.mean_after_cost_return,
        max_drawdown=max_drawdown,
        holdout_sha256=holdout_sha256,
        forward_paper_sha256=forward_paper_sha256,
        calibration_sha256=calibration_sha256,
        execution_stress_sha256=execution_stress_sha256,
        trial_register_sha256=trial_register_sha256,
    )
