"""Synthetic model/input tests, never a real model or market qualification."""
from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from threading import Barrier

import pytest

from quant_ai.analytics.learning_monitor import observe_probability_drift
from quant_ai.learning import shadow
from quant_ai.learning.contracts import TrainingDatasetManifest
from quant_ai.learning.outcomes import ForecastOutcomeJournal
from quant_ai.learning.training import execute_training

NOW = datetime(2026, 9, 17, 10, tzinfo=timezone.utc)


def bundle(**changes):
    model = {"schema": shadow.MODEL_SCHEMA, "feature_names": ["trend"],
             "coefficients": ["1"], "intercept": "0", "horizon_seconds": 60,
             "maximum_feature_age_seconds": 120, "cost_policy_id": "synthetic-costs",
             "cost_policy_sha256": "c" * 64, "event": shadow.EVENT}
    model.update(changes)
    raw = json.dumps(model).encode()
    data = TrainingDatasetManifest("synthetic-data", NOW - timedelta(days=3), 60,
        ("synthetic-source",), "a" * 64, shadow.feature_schema_digest(model["feature_names"]),
        shadow.label_schema_digest(model), "synthetic-costs", "synthetic-adjustment")
    artifact, run = execute_training(data, run_id="synthetic-training", candidate_id="test-candidate",
        model_family="decimal_logistic", code_sha256="b" * 64, configuration_sha256="d" * 64,
        seed=1, trainer=lambda dataset, seed: raw, trained_at=NOW - timedelta(days=2))
    return shadow.ShadowModelBundle(data, run, artifact)


def features(now=NOW, **changes):
    value = {"schema": shadow.INPUT_SCHEMA, "subject": "INFY",
             "observed_at": now.isoformat(), "available_at": now.isoformat(),
             "source_ids": ["synthetic-source"], "values": {"trend": "0"}}
    value.update(changes)
    return value


def count(writer, table="probability_forecasts"):
    return writer.journal.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_numeric_inference_matches_hand_computed_probabilities():
    zero = shadow.predict(bundle(), features(), now=NOW)[0]
    assert zero == D("0.5")
    high = shadow.predict(bundle(), features(values={"trend": str(D(9).ln())}), now=NOW)[0]
    low = shadow.predict(bundle(), features(values={"trend": str(-D(9).ln())}), now=NOW)[0]
    assert abs(high - D("0.9")) < D("1e-26")
    assert abs(low - D("0.1")) < D("1e-26")


@pytest.mark.parametrize("change", [
    {"schema": "pickle"}, {"event": "confidence"}, {"feature_names": []},
    {"feature_names": ["trend", "trend"], "coefficients": ["1", "1"]},
    {"coefficients": []}, {"coefficients": [1]}, {"coefficients": ["NaN"]},
    {"intercept": "Infinity"}, {"intercept": "1e13"},
    {"horizon_seconds": True}, {"horizon_seconds": 0}, {"horizon_seconds": 86401},
    {"maximum_feature_age_seconds": 0}, {"cost_policy_sha256": "unknown"},
    {"cost_policy_id": "other-costs"}, {"unexpected": "field"},
])
def test_unsupported_artifacts_are_refused(change):
    with pytest.raises((ValueError, TypeError, ArithmeticError)):
        bundle(**change)


@pytest.mark.parametrize("field,value", [
    ("artifact", b"{}"),
    ("dataset", "not-manifest"),
])
def test_bundle_identity_is_not_inferred(field, value):
    with pytest.raises((ValueError, TypeError)):
        replace(bundle(), **{field: value})


def test_training_and_dataset_bindings_are_rechecked():
    original = bundle()
    for changed in (
        replace(original.training_run, dataset_manifest_sha256=None),
        replace(original.training_run, model_family="arbitrary_python"),
        replace(original.training_run, artifact_sha256="e" * 64),
    ):
        with pytest.raises(ValueError):
            replace(original, training_run=changed)
    with pytest.raises(ValueError, match="dataset_binding"):
        replace(original, dataset=replace(original.dataset, row_count=61))


@pytest.mark.parametrize("change", [
    {"values": {"trend": 0.32}}, {"values": {"trend": "NaN"}},
    {"values": {"trend": "Infinity"}}, {"values": {"trend": "1e13"}},
    {"values": {"trend": "61"}}, {"values": {"confidence": "0.32"}},
    {"available_at": (NOW + timedelta(seconds=1)).isoformat()},
    {"observed_at": (NOW + timedelta(seconds=1)).isoformat()},
    {"observed_at": (NOW - timedelta(seconds=121)).isoformat()},
    {"observed_at": NOW.replace(tzinfo=None).isoformat()},
    {"source_ids": []}, {"source_ids": ["same", "same"]},
    {"source_ids": ["bad\nsource"]}, {"subject": "../invalid symbol"},
    {"schema": "raw_consensus"}, {"stance": "NEUTRAL"},
])
def test_invalid_inputs_leave_no_partial_forecast(tmp_path, change):
    with shadow.ShadowForecastWriter(tmp_path / "shadow.db", tenant_id="ghost", clock=lambda: NOW) as writer:
        with pytest.raises((ValueError, TypeError, ArithmeticError)):
            writer.record(bundle(), features(**change), pair_id="one")
        assert count(writer) == 0
        assert count(writer, "shadow_model_bindings") == 0
        assert count(writer, "shadow_forecast_inputs") == 0


def test_future_trained_model_is_not_usable(tmp_path):
    original = bundle()
    future = replace(original, training_run=replace(original.training_run, trained_at=NOW + timedelta(seconds=1)))
    with shadow.ShadowForecastWriter(tmp_path / "shadow.db", tenant_id="ghost", clock=lambda: NOW) as writer:
        with pytest.raises(ValueError, match="training_time"):
            writer.record(future, features(), pair_id="one")
        assert count(writer) == 0


def test_record_retry_restart_and_source_replay(tmp_path):
    path = tmp_path / "shadow.db"
    with shadow.ShadowForecastWriter(path, tenant_id="ghost", clock=lambda: NOW) as writer:
        forecast = writer.record(bundle(), features(), pair_id="one")
        writer.clock = lambda: NOW + timedelta(days=1)
        assert writer.record(bundle(), features(), pair_id="one") == forecast
        assert count(writer) == 1
        assert shadow.validate_shadow_lineage(writer.journal.db, tenant_id="ghost")["verified_forecasts"] == 1
    with shadow.ShadowForecastWriter(path, tenant_id="ghost") as restarted:
        assert restarted.record(bundle(), features(), pair_id="one") == forecast
        assert count(restarted) == 1
        assert count(restarted, "forecast_outcomes") == 0


def test_changed_pair_or_candidate_does_not_rewrite_history(tmp_path):
    with shadow.ShadowForecastWriter(tmp_path / "shadow.db", tenant_id="ghost", clock=lambda: NOW) as writer:
        writer.record(bundle(), features(), pair_id="one")
        with pytest.raises(ValueError, match="pair_input_reused"):
            writer.record(bundle(), features(values={"trend": "1"}), pair_id="one")
        with pytest.raises(ValueError, match="candidate_artifact_reused"):
            writer.record(bundle(intercept="1"), features(), pair_id="two")
        assert count(writer) == 1


def test_forecast_and_lineage_commit_or_rollback_together(tmp_path, monkeypatch):
    with shadow.ShadowForecastWriter(tmp_path / "shadow.db", tenant_id="ghost", clock=lambda: NOW) as writer:
        original = writer.journal._insert_forecast
        def interrupted(item):
            original(item)
            raise RuntimeError("synthetic interruption")
        monkeypatch.setattr(writer.journal, "_insert_forecast", interrupted)
        with pytest.raises(RuntimeError, match="synthetic interruption"):
            writer.record(bundle(), features(), pair_id="one")
        assert count(writer) == 0
        assert count(writer, "shadow_model_bindings") == 0
        assert count(writer, "shadow_forecast_inputs") == 0
        monkeypatch.setattr(writer.journal, "_insert_forecast", original)
        writer.record(bundle(), features(), pair_id="one")
        assert count(writer) == 1


def test_existing_unscoped_journal_is_not_silently_adopted(tmp_path):
    path = tmp_path / "existing.db"
    with ForecastOutcomeJournal(path):
        pass
    before = path.read_bytes()
    with pytest.raises((ValueError, sqlite3.Error)):
        shadow.ShadowForecastWriter(path, tenant_id="ghost")
    assert path.read_bytes() == before


def test_wrong_tenant_is_refused_before_modifying_the_store(tmp_path):
    path = tmp_path / "shadow.db"
    with shadow.ShadowForecastWriter(path, tenant_id="ghost", clock=lambda: NOW) as writer:
        writer.record(bundle(), features(), pair_id="one")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="tenant_binding"):
        shadow.ShadowForecastWriter(path, tenant_id="other")
    assert path.read_bytes() == before


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "public"])
def test_untrusted_journal_file_shape_is_refused(tmp_path, kind):
    import os
    path = tmp_path / "shadow.db"
    with shadow.ShadowForecastWriter(path, tenant_id="ghost"):
        pass
    target = path
    if kind == "public":
        path.chmod(0o644)
    else:
        target = tmp_path / "alias.db"
        if kind == "symlink":
            target.symlink_to(path)
        else:
            os.link(path, target)
    with pytest.raises(ValueError, match="private_unaliased_file_required"):
        shadow.ShadowForecastWriter(target, tenant_id="ghost")


def test_separate_connections_retry_the_same_pair_once(tmp_path):
    path = tmp_path / "shadow.db"
    with (
        shadow.ShadowForecastWriter(path, tenant_id="ghost", clock=lambda: NOW) as a,
        shadow.ShadowForecastWriter(path, tenant_id="ghost", clock=lambda: NOW) as b,
    ):
        barrier = Barrier(2)
        def record(writer):
            barrier.wait(timeout=5)
            return writer.record(bundle(), features(), pair_id="same")
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(record, w) for w in (a, b)]
            assert futures[0].result(timeout=10) == futures[1].result(timeout=10)
        assert count(a) == 1


@pytest.mark.parametrize("table", sorted(shadow.TABLES))
def test_lineage_records_are_append_only(tmp_path, table):
    with shadow.ShadowForecastWriter(tmp_path / "shadow.db", tenant_id="ghost", clock=lambda: NOW) as writer:
        writer.record(bundle(), features(), pair_id="one")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"), writer.journal.db as db:
            db.execute(f"DELETE FROM {table}")


def test_rehashed_forged_probability_fails_model_replay(tmp_path):
    with shadow.ShadowForecastWriter(tmp_path / "shadow.db", tenant_id="ghost", clock=lambda: NOW) as writer:
        forecast = writer.record(bundle(), features(), pair_id="one")
        fake = replace(forecast, probability_positive_after_cost=D("0.99"))
        with writer.journal.db as db:
            db.execute("DROP TRIGGER probability_forecasts_update_blocked")
            db.execute("UPDATE probability_forecasts SET probability=?,payload_sha256=?", ("0.99", shadow._hash(ForecastOutcomeJournal._forecast_payload(fake))))
        with pytest.raises(ValueError, match="forecast_replay_mismatch"):
            shadow.validate_shadow_lineage(writer.journal.db, tenant_id="ghost")
        with pytest.raises(ValueError):
            writer.record(bundle(), features(), pair_id="two")
        assert count(writer) == 1


def test_journal_limit_refuses_the_next_record_not_a_future_reader(tmp_path, monkeypatch):
    monkeypatch.setattr(shadow, "MAX_FORECASTS", 1)
    with shadow.ShadowForecastWriter(tmp_path / "shadow.db", tenant_id="ghost", clock=lambda: NOW) as writer:
        writer.record(bundle(), features(), pair_id="one")
        with pytest.raises(ValueError, match="journal_row_limit"):
            writer.record(bundle(), features(), pair_id="two")
        assert count(writer) == 1


def test_probability_outcome_and_existing_monitor_end_to_end(tmp_path):
    journal_path = tmp_path / "shadow.db"
    start = NOW - timedelta(days=1)
    recent = NOW - timedelta(hours=2)
    with shadow.ShadowForecastWriter(journal_path, tenant_id="ghost") as writer:
        for group, at in enumerate((start, recent)):
            for index in range(30):
                moment = at + timedelta(minutes=index * 2)
                writer.clock = lambda moment=moment: moment
                forecast = writer.record(bundle(), features(moment), pair_id=f"{group}-{index}")
                writer.journal.resolve(forecast.forecast_id, gross_return=D("0.01"), cost_return=D("0.001"), resolved_at=forecast.resolve_after)
        assert count(writer) == 60
    config = {"schema": "pramana.learning_monitor.v1", "tenant_id": "ghost",
        "journal_filename": "shadow.db", "candidate_id": "test-candidate",
        "model_artifact_sha256": bundle().training_run.artifact_sha256,
        "cost_policy_sha256": "c" * 64, "reference_start": start.isoformat(),
        "reference_end": (start + timedelta(hours=1)).isoformat(), "recent_start": recent.isoformat(),
        "maximum_recent_age_seconds": 86400, "require_shadow_lineage": True}
    path = tmp_path / "learning-monitor.json"
    path.write_text(json.dumps(config)); path.chmod(0o600)
    report = observe_probability_drift(path, tenant_id="ghost", now=NOW)
    assert report["status"] == "healthy", report
    assert report["reference_samples"] == report["recent_samples"] == 30
    assert report["shadow_lineage"]["verified_forecasts"] == 60
    config["tenant_id"] = "other"
    path.write_text(json.dumps(config))
    assert observe_probability_drift(path, tenant_id="other", now=NOW)["status"] == "refused"


@pytest.mark.parametrize("after_commit", [False, True])
def test_interrupted_process_never_leaves_half_a_forecast(tmp_path, after_commit):
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    child = """
import os, sys
from pathlib import Path
from test_shadow_forecast_lineage import bundle, features, NOW
from quant_ai.learning.shadow import ShadowForecastWriter
writer = ShadowForecastWriter(Path(sys.argv[1]), tenant_id='ghost', clock=lambda: NOW)
if sys.argv[2] == 'False':
    insert = writer.journal._insert_forecast
    def interrupted(item):
        insert(item)
        os._exit(17)
    writer.journal._insert_forecast = interrupted
writer.record(bundle(), features(), pair_id='one')
os._exit(17)
"""
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(root / "src"), str(root / "tests")]),
           "TRADING_LIVE_MONEY_ACTIVE": "false", "PYTHONDONTWRITEBYTECODE": "1"}
    path = tmp_path / "crashed.db"
    result = subprocess.run([sys.executable, "-B", "-c", child, str(path), str(after_commit)],
                            capture_output=True, text=True, env=env, timeout=20, check=False)
    assert result.returncode == 17, result.stderr
    with shadow.ShadowForecastWriter(path, tenant_id="ghost", clock=lambda: NOW) as writer:
        assert count(writer) == int(after_commit)
        assert count(writer, "shadow_forecast_inputs") == int(after_commit)
        writer.record(bundle(), features(), pair_id="one")
        assert count(writer) == count(writer, "shadow_forecast_inputs") == 1
        assert shadow.validate_shadow_lineage(writer.journal.db, tenant_id="ghost")["verified_forecasts"] == 1


def test_actual_command_uses_real_clock_and_outputs_only_shadow_evidence(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    model_file = tmp_path / "bundle.json"
    input_file = tmp_path / "features.json"
    model_file.write_text(json.dumps(bundle().payload())); model_file.chmod(0o600)
    moment = datetime.now(timezone.utc)
    input_file.write_text(json.dumps(features(moment))); input_file.chmod(0o600)
    path = tmp_path / "new-shadow.db"
    command = [sys.executable, "-B", str(root / "scripts/record_shadow_forecast.py"),
               "--bundle", str(model_file), "--features", str(input_file), "--journal", str(path),
               "--tenant", "ghost", "--pair-id", "one"]
    env = {**os.environ, "TRADING_LIVE_MONEY_ACTIVE": "false"}
    result = subprocess.run(command, capture_output=True, text=True, env=env, timeout=20, check=False)
    assert result.returncode == 0, result.stderr
    record = json.loads(result.stdout)
    assert record["mode"] == "SHADOW" and record["trading_authorized"] is False
    assert D(record["probability_positive_after_cost"]) == D("0.5")
    assert "synthetic-source" not in result.stdout and "coefficients" not in result.stdout
    input_file.chmod(0o644)
    rejected = subprocess.run(command, capture_output=True, text=True, env=env, timeout=20, check=False)
    assert rejected.returncode == 2 and rejected.stdout == ""
    assert str(tmp_path) not in rejected.stderr
    with shadow.ShadowForecastWriter(path, tenant_id="ghost") as writer:
        assert count(writer) == 1


def test_model_feature_and_label_schema_cannot_be_rebound():
    from quant_ai.learning.training import training_dataset_digest

    original = bundle()
    for field in ("feature_schema_sha256", "label_schema_sha256"):
        data = replace(original.dataset, **{field: "e" * 64})
        run = replace(original.training_run, dataset_manifest_sha256=training_dataset_digest(data))
        with pytest.raises(ValueError, match="schema_binding"):
            shadow.ShadowModelBundle(data, run, original.artifact)


def test_missing_lineage_is_not_silently_repaired(tmp_path):
    with shadow.ShadowForecastWriter(tmp_path / "shadow.db", tenant_id="ghost", clock=lambda: NOW) as writer:
        writer.record(bundle(), features(), pair_id="one")
        with writer.journal.db as db:
            db.execute("DROP TRIGGER shadow_forecast_inputs_delete_blocked")
            db.execute("DELETE FROM shadow_forecast_inputs")
        with pytest.raises(ValueError, match="lineage"):
            writer.record(bundle(), features(), pair_id="two")
        assert count(writer) == 1
        assert count(writer, "shadow_forecast_inputs") == 0


def test_changed_lineage_candidate_column_is_rejected(tmp_path):
    with shadow.ShadowForecastWriter(tmp_path / "shadow.db", tenant_id="ghost", clock=lambda: NOW) as writer:
        writer.record(bundle(), features(), pair_id="one")
        with writer.journal.db as db:
            db.execute("DROP TRIGGER shadow_forecast_inputs_update_blocked")
            db.execute("UPDATE shadow_forecast_inputs SET candidate_id='other'")
        with pytest.raises(ValueError, match="lineage_binding"):
            shadow.validate_shadow_lineage(writer.journal.db, tenant_id="ghost")


def test_byte_valid_but_replaced_store_is_not_written_via_old_handle(tmp_path):
    path = tmp_path / "shadow.db"
    with shadow.ShadowForecastWriter(path, tenant_id="ghost", clock=lambda: NOW) as writer:
        path.rename(tmp_path / "old.db")
        path.write_bytes(b"not our selected store"); path.chmod(0o600)
        with pytest.raises(ValueError, match="journal_path_replaced"):
            writer.record(bundle(), features(), pair_id="one")
        assert path.read_bytes() == b"not our selected store"


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}', b'{"value":NaN}', b'x' * 65537])
def test_strict_decoder_refuses_ambiguous_or_oversized_input(raw):
    with pytest.raises(ValueError):
        shadow.decode(raw)


def test_mandatory_lineage_monitor_cannot_fall_back_to_unscoped_journal(tmp_path):
    from test_learning_monitor import NOW as monitor_now
    from test_learning_monitor import setup_monitor

    path, _, _ = setup_monitor(tmp_path)
    original = json.loads(path.read_text())
    original["require_shadow_lineage"] = True
    path.write_text(json.dumps(original))
    result = observe_probability_drift(path, tenant_id=original["tenant_id"], now=monitor_now)
    assert result["status"] == "refused"
    original["require_shadow_lineage"] = False
    path.write_text(json.dumps(original))
    assert observe_probability_drift(path, tenant_id=original["tenant_id"], now=monitor_now)["status"] == "refused"


def test_changed_artifact_bytes_refused_even_with_same_shape():
    original = bundle()
    model = json.loads(original.artifact)
    model["intercept"] = "1"
    with pytest.raises(ValueError, match="artifact_binding"):
        replace(original, artifact=json.dumps(model).encode())


def test_input_not_available_at_decision_is_rejected():
    with pytest.raises(ValueError, match="input_time"):
        shadow.predict(bundle(), features(available_at=(NOW + timedelta(seconds=1)).isoformat()), now=NOW)


def test_logit_domain_refuses_overflow_instead_of_saturating():
    with pytest.raises(ValueError, match="logit_outside_supported_domain"):
        shadow.predict(bundle(), features(values={"trend": "61"}), now=NOW)


def test_forecast_is_rolled_back_when_its_source_insert_fails(tmp_path):
    with shadow.ShadowForecastWriter(tmp_path / "shadow.db", tenant_id="ghost", clock=lambda: NOW) as writer:
        with writer.journal.db as db:
            db.execute("CREATE TRIGGER synthetic_source_failure BEFORE INSERT ON shadow_forecast_inputs BEGIN SELECT RAISE(ABORT,'synthetic source failure'); END")
        with pytest.raises(sqlite3.IntegrityError, match="synthetic source failure"):
            writer.record(bundle(), features(), pair_id="one")
        assert count(writer) == 0
        assert count(writer, "shadow_model_bindings") == 0
        assert count(writer, "shadow_forecast_inputs") == 0


def test_clock_callback_cannot_substitute_supplied_model_or_features(tmp_path):
    selected = bundle()
    vector = features()
    replacement = bundle(intercept="1")
    def clock():
        object.__setattr__(selected, "artifact", replacement.artifact)
        object.__setattr__(selected, "training_run", replacement.training_run)
        vector["values"]["trend"] = "1"
        return NOW
    with shadow.ShadowForecastWriter(tmp_path / "shadow.db", tenant_id="ghost", clock=clock) as writer:
        recorded = writer.record(selected, vector, pair_id="one")
        assert recorded.probability_positive_after_cost == D("0.5")
        assert shadow.validate_shadow_lineage(writer.journal.db, tenant_id="ghost")["verified_forecasts"] == 1
