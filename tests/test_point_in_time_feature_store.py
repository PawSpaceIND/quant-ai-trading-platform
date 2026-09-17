from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant_ai.features.store import (
    FeatureObservation,
    FeatureRequirement,
    PointInTimeFeatureStore,
)

NOW = datetime(2026, 9, 16, 10, tzinfo=timezone.utc)
D = Decimal


def observation(obs_id, value, observed, available, *, feature="pe", source="fundamentals", schema="fund-v1"):
    return FeatureObservation(obs_id, "INFY", feature, D(value), observed, available, source, schema)


def requirement(feature="pe", age=86400 * 365, sources=frozenset({"fundamentals"}), schema="fund-v1"):
    return FeatureRequirement(feature, age, sources, schema)


def test_later_revision_cannot_leak_into_earlier_training_or_decision_snapshot(tmp_path):
    with PointInTimeFeatureStore(tmp_path / "features.sqlite") as store:
        period = NOW - timedelta(days=30)
        store.append(observation("original", "20", period, NOW - timedelta(days=10)))
        store.append(observation("revision", "18", period, NOW + timedelta(days=1)))
        before = store.snapshot(subject="INFY", as_of=NOW, requirements=(requirement(),))
        after = store.snapshot(
            subject="INFY", as_of=NOW + timedelta(days=2), requirements=(requirement(),)
        )
        assert before.values()["pe"] == D("20")
        assert after.values()["pe"] == D("18")
        assert before.sha256 != after.sha256


def test_latest_observation_not_latest_revision_time_wins_then_latest_available_revision_of_that_fact(tmp_path):
    with PointInTimeFeatureStore(tmp_path / "features.sqlite") as store:
        store.append(observation("old", "20", NOW - timedelta(days=2), NOW - timedelta(days=2)))
        store.append(observation("new", "21", NOW - timedelta(days=1), NOW - timedelta(hours=12)))
        store.append(observation("old-revised", "19", NOW - timedelta(days=2), NOW - timedelta(hours=1)))
        snap = store.snapshot(subject="INFY", as_of=NOW, requirements=(requirement(),))
        assert snap.points[0].observation_id == "new"
        assert snap.values()["pe"] == D("21")


def test_stale_missing_wrong_source_or_wrong_schema_refuses(tmp_path):
    with PointInTimeFeatureStore(tmp_path / "features.sqlite") as store:
        store.append(observation("x", "20", NOW - timedelta(hours=2), NOW - timedelta(hours=2)))
        with pytest.raises(ValueError, match="feature_stale"):
            store.snapshot(subject="INFY", as_of=NOW, requirements=(requirement(age=60),))
        with pytest.raises(ValueError, match="feature_unavailable"):
            store.snapshot(
                subject="INFY", as_of=NOW,
                requirements=(requirement(sources=frozenset({"other"})),),
            )
        with pytest.raises(ValueError, match="feature_unavailable"):
            store.snapshot(
                subject="INFY", as_of=NOW, requirements=(requirement(schema="fund-v2"),)
            )


def test_observation_idempotency_append_only_and_hash_tamper_detection(tmp_path):
    path = tmp_path / "features.sqlite"
    with PointInTimeFeatureStore(path) as store:
        item = observation("x", "20", NOW - timedelta(hours=1), NOW - timedelta(hours=1))
        store.append(item)
        store.append(item)
        with pytest.raises(ValueError, match="payload_mismatch"):
            store.append(observation("x", "21", item.observed_at, item.available_at))
        with pytest.raises(Exception, match="append-only"):
            store.db.execute("DELETE FROM feature_observations")
        # Direct projection tamper is blocked, but if external corruption bypasses triggers,
        # the stored content hash is independently checked on read.
        store.db.execute("DROP TRIGGER feature_observations_update_blocked")
        store.db.execute("UPDATE feature_observations SET value='999' WHERE observation_id='x'")
        with pytest.raises(ValueError, match="hash_mismatch"):
            store.snapshot(subject="INFY", as_of=NOW, requirements=(requirement(),))


def test_training_materialization_replays_each_decision_cutoff_independently(tmp_path):
    with PointInTimeFeatureStore(tmp_path / "features.sqlite") as store:
        store.append(observation("t1", "20", NOW - timedelta(hours=3), NOW - timedelta(hours=3)))
        store.append(observation("t2", "21", NOW - timedelta(hours=1), NOW - timedelta(hours=1)))
        rows = store.materialize(
            subject="INFY",
            decision_times=(NOW - timedelta(hours=2), NOW),
            requirements=(requirement(age=86400),),
        )
        assert [row.values()["pe"] for row in rows] == [D("20"), D("21")]
        with pytest.raises(ValueError, match="strictly_ordered"):
            store.materialize(
                subject="INFY", decision_times=(NOW, NOW - timedelta(hours=1)),
                requirements=(requirement(),),
            )
