"""Read-only, synthetic after-cost forecast monitoring through the existing report."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant_ai.analytics import decision_quality
from quant_ai.analytics import learning_monitor as monitor
from quant_ai.learning.outcomes import ForecastOutcomeJournal, ProbabilityForecast

D = Decimal
START = datetime(2026, 9, 1, tzinfo=timezone.utc)
NOW = START + timedelta(days=1, hours=8)


def setup_monitor(tmp_path, *, count=30, loss=False, overlap=False):
    path = tmp_path / "learning-monitor.json"
    config = {
        "schema": monitor.SCHEMA, "tenant_id": "pilot", "journal_filename": "forecasts.sqlite",
        "candidate_id": "synthetic-candidate", "model_artifact_sha256": "a" * 64,
        "cost_policy_sha256": "b" * 64, "reference_start": START.isoformat(),
        "reference_end": (START + timedelta(hours=6)).isoformat(),
        "recent_start": (START + timedelta(days=1)).isoformat(), "maximum_recent_age_seconds": 86400,
    }
    path.write_text(json.dumps(config)); path.chmod(0o600)
    dbpath = tmp_path / config["journal_filename"]
    with ForecastOutcomeJournal(dbpath) as journal:
        for window in range(2):
            for index in range(count):
                at = START + timedelta(days=window, minutes=index * (1 if overlap else 10))
                name = f"{window}-{index}"
                item = ProbabilityForecast(name, name, config["candidate_id"], "INFY", D("0.8"),
                    at, at + timedelta(minutes=5), "c" * 64, "a" * 64, "b" * 64, "ranging")
                journal.record_forecast(item)
                journal.resolve(name, gross_return=D("-0.01" if loss and window else "0.02"),
                                cost_return=D("0.001"), resolved_at=at + timedelta(minutes=5))
    return path, dbpath, config


def read(path, now=NOW):
    return monitor.observe_probability_drift(path, tenant_id="pilot", now=now)


def test_missing_configuration_does_not_create_a_journal_or_a_score(tmp_path):
    before = list(tmp_path.iterdir())
    report = read(tmp_path / "learning-monitor.json")
    assert report["status"] == "unconfigured" and report["healthy"] is None
    assert report["reference_samples"] == report["recent_samples"] == 0
    assert list(tmp_path.iterdir()) == before


def test_genuine_probability_contract_reuses_existing_drift_and_performance(tmp_path, monkeypatch):
    path, dbpath, _ = setup_monitor(tmp_path)
    original = dbpath.read_bytes()
    seen = []
    real = monitor.evaluate_probability_drift
    def capture(reference, recent):
        seen.append((reference, recent))
        return real(reference, recent)
    monkeypatch.setattr(monitor, "evaluate_probability_drift", capture)
    report = read(path)
    assert report["status"] == "healthy" and report["healthy"] is True
    assert report["reference_samples"] == report["recent_samples"] == 30
    assert D(report["reference_brier"]) == D("0.04")
    assert D(report["recent_after_cost_expectancy"]) == D("0.019")
    assert len(seen) == 1 and seen[0][0].samples == 30
    assert dbpath.read_bytes() == original


def test_degraded_after_cost_outcomes_are_reported_not_promoted(tmp_path):
    path, _, _ = setup_monitor(tmp_path, loss=True)
    report = read(path)
    assert report["status"] == "degraded" and report["healthy"] is False
    assert "brier_score_degraded" in report["reasons"]
    assert "recent_after_cost_expectancy_not_positive" in report["reasons"]


@pytest.mark.parametrize("count", [0, 1, 29])
def test_minimum_window_sample_is_not_lowered(tmp_path, count):
    path, _, _ = setup_monitor(tmp_path, count=count)
    result = read(path)
    assert result["status"] == "insufficient_sample" and result["healthy"] is None
    assert "recent_brier" not in result


def test_overlapping_windows_cannot_multiply_eligible_samples(tmp_path):
    path, _, _ = setup_monitor(tmp_path, overlap=True)
    result = read(path)
    assert result["reference_raw"] == result["recent_raw"] == 30
    assert result["reference_samples"] == result["recent_samples"] == 6
    assert result["excluded_overlaps"] == 48
    assert result["status"] == "insufficient_sample"


@pytest.mark.parametrize("field,value", [
    ("schema", "wrong"), ("tenant_id", "another-tenant"),
    ("journal_filename", "../foreign.sqlite"), ("journal_filename", "/tmp/foreign.sqlite"),
    ("candidate_id", ""), ("model_artifact_sha256", "d" * 64),
    ("cost_policy_sha256", "d" * 64), ("model_artifact_sha256", "not-a-digest"),
    ("reference_start", "2026-09-01T00:00:00"),
    ("recent_start", START.isoformat()), ("recent_start", (NOW + timedelta(days=1)).isoformat()),
    ("maximum_recent_age_seconds", 0), ("maximum_recent_age_seconds", True),
])
def test_invalid_configuration_and_mixed_artifacts_refuse(tmp_path, field, value):
    path, _, config = setup_monitor(tmp_path)
    config[field] = value
    path.write_text(json.dumps(config))
    result = read(path)
    assert result["status"] == "refused" and result["healthy"] is None


def test_missing_named_journal_is_not_created(tmp_path):
    path, dbpath, _ = setup_monitor(tmp_path)
    dbpath.unlink()
    assert read(path)["status"] == "refused"
    assert not dbpath.exists()


def test_stale_recent_evidence_is_not_healthy(tmp_path):
    path, _, _ = setup_monitor(tmp_path)
    assert read(path, NOW + timedelta(days=2))["status"] == "refused"


@pytest.mark.parametrize("mode", ["public", "symlink", "duplicate"])
def test_private_configuration_and_unambiguous_fields_are_required(tmp_path, mode):
    path, _, config = setup_monitor(tmp_path)
    if mode == "public":
        path.chmod(0o644)
    elif mode == "symlink":
        other = tmp_path / "original.json"
        path.rename(other)
        path.symlink_to(other)
    else:
        raw = json.dumps(config)
        path.write_text(raw[:-1] + ',"tenant_id":"pilot"}')
    assert read(path)["status"] == "refused"


@pytest.mark.parametrize("field,value", [
    ("probability", "NaN"), ("probability", "1.2"),
    ("after_cost_return", "123"), ("cost_return", "-1"),
    ("positive_after_cost", 0), ("payload_sha256", "corrupt"),
    ("resolved_at", START.isoformat()), ("resolved_at", (NOW + timedelta(days=1)).isoformat()),
])
def test_corrupt_probability_or_outcome_cannot_produce_a_drift_score(tmp_path, field, value):
    path, dbpath, _ = setup_monitor(tmp_path)
    import sqlite3
    table = "probability_forecasts" if field in {"probability", "payload_sha256"} else "forecast_outcomes"
    with sqlite3.connect(dbpath) as db:
        db.execute(f"DROP TRIGGER {table}_update_blocked")
        db.execute(f"UPDATE {table} SET {field}=? WHERE forecast_id='1-0'", (value,))
    assert read(path)["status"] == "refused"


def test_no_neutral_consensus_rows_are_fabricated_as_probability_forecasts(tmp_path):
    rows = [{"stance": "NEUTRAL", "confidence": "0.32", "forward_return_60m": "0.01"} for _ in range(90)]
    assert decision_quality.directional(rows)["evaluated"] == 0
    assert decision_quality.calibration(rows)["brier_score"] is None
    assert read(tmp_path / "learning-monitor.json")["status"] == "unconfigured"


def test_existing_daemon_report_and_alert_paths_publish_the_observation(tmp_path):
    from test_pilot_closure import runner_for

    from quant_ai.notifications.trading import JsonlFileSink, TradingNotificationDispatcher

    path, _, _ = setup_monitor(tmp_path, loss=True)
    (tmp_path / "runtime").mkdir()
    runner = runner_for(tmp_path / "runtime")
    daemon = runner.daemon
    daemon.clock = lambda: NOW
    daemon.decision_quality_report_path = path.parent / "decision-quality.json"
    alertfile = tmp_path / "alerts.jsonl"
    daemon.notifications = TradingNotificationDispatcher((JsonlFileSink(alertfile),))
    try:
        daemon._write_decision_quality(NOW)
        report = json.loads(daemon.decision_quality_report_path.read_text())
        assert report["probability_drift"]["status"] == "degraded"
        assert report["learning_feedback"]["status"] == "no_closed_entries"
        assert any("Probability drift: degraded" in line for line in report["limitations"])
        assert "LEARNING_EVIDENCE_DEGRADED" in alertfile.read_text()
        daemon._write_decision_quality(NOW)
        assert len(alertfile.read_text().splitlines()) == 1
        assert not daemon.kill_switch.engaged
        assert not daemon.tracker.broker.ledger_entries(daemon.tenant_id)
    finally:
        daemon.tracker.broker.close()


def test_monitoring_does_not_rewrite_source_files(tmp_path):
    path, dbpath, _ = setup_monitor(tmp_path)
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (path, dbpath)}
    assert read(path)["status"] == "healthy"
    assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before} == before
