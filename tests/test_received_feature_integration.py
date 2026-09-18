"""Run receipt-backed observations through the actual training and shadow components."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from test_received_feature_ingestion import T, batch, grant, record, send

from quant_ai.features.store import FeatureRequirement
from quant_ai.learning.feature_dataset import build_training_package, fit_training_package
from quant_ai.learning.ingestion import ReceivedFeatureStore, _grant_payload, canonical
from quant_ai.learning.shadow import INPUT_SCHEMA, ShadowForecastWriter, validate_shadow_lineage

ROOT = Path(__file__).resolve().parents[1]


def data_plan(count=40):
    def rule(name, schema, category):
        return {"feature": name, "source_id": "recorded", "schema_id": schema,
                "max_age_seconds": 60, "category": category}
    return {"schema": "pramana.feature_training_plan.v1", "partition": "training",
        "dataset_id": "receipt-fixture", "cutoff": (T + timedelta(hours=2)).isoformat(),
        "horizon_seconds": 60, "maximum_feature_age_seconds": 60,
        "cost_policy_id": "synthetic-cost", "cost_policy_sha256": "c" * 64,
        "adjustment_policy_id": "unadjusted-test",
        "features": [rule("signal", "feature:v1", "MARKET")],
        "price": rule("price", "price:unadjusted-test", "MARKET"),
        "cost": rule("cost", "cost_fraction:" + "c" * 64, "BROKER"),
        "decisions": [{"row_id": str(i), "subject": record()["subject"],
                       "decision_at": (T + timedelta(minutes=2*i, seconds=1)).isoformat()}
                      for i in range(count)]}


def populate(path, count=40, late=False):
    with ReceivedFeatureStore(path, tenant_id="ghost") as store:
        for i in range(count):
            observed = T + timedelta(minutes=2*i)
            decision = observed + timedelta(seconds=1)
            due = decision + timedelta(minutes=1)
            rows = [record(record_id=f"signal-{i}", value="1" if i % 2 else "-1", observed_at=observed.isoformat()),
                    record(record_id=f"price-{i}", feature="price", schema_id="price:unadjusted-test", value="100", observed_at=observed.isoformat()),
                    record(record_id=f"cost-{i}", feature="cost", schema_id="cost_fraction:" + "c" * 64,
                           category="BROKER", value="0.001", observed_at=observed.isoformat())]
            send(store, batch(rows, batch_id=f"input-{i}"), at=T + timedelta(days=1) if late else decision)
            send(store, batch([record(record_id=f"end-{i}", feature="price", schema_id="price:unadjusted-test",
                 value="102" if i % 2 else "98", observed_at=due.isoformat())], batch_id=f"end-{i}"),
                 at=T + timedelta(days=1) if late else due + timedelta(seconds=1))


def test_receipts_to_training_fit_and_shadow_forecast(tmp_path):
    path = tmp_path / "observations.db"
    populate(path)
    raw = build_training_package(path, canonical(data_plan()), source_grants=(grant(),), now=T + timedelta(hours=3))
    fitted = fit_training_package(raw, source_grants=(grant(),), run_id="receipt-run", candidate_id="receipt-model",
                                  clock=lambda: T + timedelta(hours=3))
    assert fitted.diagnostics["positive_after_cost_rows"] == 20
    now = T + timedelta(hours=4)
    with ReceivedFeatureStore(path, tenant_id="ghost") as store:
        send(store, batch([record(record_id="later", observed_at=now.isoformat())], batch_id="later"), at=now)
        snapshot = store.snapshot(subject=record()["subject"], as_of=now,
            requirements=(FeatureRequirement("signal", 60, frozenset({"recorded"}), "feature:v1"),))
    point = snapshot.points[0]
    vector = {"schema": INPUT_SCHEMA, "subject": snapshot.subject,
        "observed_at": point.observed_at.isoformat(), "available_at": point.available_at.isoformat(),
        "source_ids": [point.source_id], "values": {"signal": str(point.value)}}
    with ShadowForecastWriter(tmp_path / "shadow.db", tenant_id="ghost", clock=lambda: now) as writer:
        first = writer.record(fitted.bundle, vector, pair_id="receipt-pair")
        assert first.probability_positive_after_cost > 0.5
        assert writer.record(fitted.bundle, vector, pair_id="receipt-pair") == first
        assert validate_shadow_lineage(writer.journal.db, tenant_id="ghost")["verified_forecasts"] == 1


def test_late_import_cannot_be_used_to_reconstruct_old_training_decision(tmp_path):
    path = tmp_path / "observations.db"
    populate(path, count=1, late=True)
    plan = data_plan(1); plan["cutoff"] = (T + timedelta(days=2)).isoformat()
    with pytest.raises(ValueError, match="feature_unavailable"):
        build_training_package(path, canonical(plan), source_grants=(grant(),), now=T + timedelta(days=2))


def run_cli(tmp_path, *, raw=None, live=False):
    source, grants, database = tmp_path / "input.json", tmp_path / "grants.json", tmp_path / "observations.db"
    current = datetime.now(timezone.utc) - timedelta(seconds=5)
    source.write_bytes(batch([record(observed_at=current.isoformat())]) if raw is None else raw)
    grants.write_bytes(canonical([_grant_payload(grant())]))
    source.chmod(0o600); grants.chmod(0o600)
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path),
           "PYTHONDONTWRITEBYTECODE": "1", "TRADING_LIVE_MONEY_ACTIVE": "true" if live else "false"}
    done = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/ingest_feature_observations.py"),
        "--input", str(source), "--grants", str(grants), "--store", str(database), "--tenant", "ghost"],
        env=env, capture_output=True, text=True, timeout=20, check=False)
    return done, database


def test_actual_cli_uses_wall_clock_and_redacts_input(tmp_path):
    before = datetime.now(timezone.utc)
    done, database = run_cli(tmp_path)
    after = datetime.now(timezone.utc)
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout)
    assert before <= datetime.fromisoformat(result["received_at"]) <= after
    assert result["new_observations"] == 1 and result["trading_authorized"] is False
    assert result["source_authenticity_verified"] is False
    assert result["past_availability_reconstructed"] is False
    assert "signal" not in done.stdout and str(tmp_path) not in done.stdout + done.stderr
    assert database.stat().st_mode & 0o077 == 0


def test_cli_refuses_live_mode_before_creating_database(tmp_path):
    done, database = run_cli(tmp_path, live=True)
    assert done.returncode == 2 and not database.exists()
    assert str(tmp_path) not in done.stderr and done.stdout == ""


def test_cli_refuses_unknown_availability_field_before_creating_database(tmp_path):
    done, database = run_cli(tmp_path, raw=batch([record(available_at=T.isoformat())]))
    assert done.returncode == 2 and not database.exists()
    assert str(tmp_path) not in done.stderr and done.stdout == ""
