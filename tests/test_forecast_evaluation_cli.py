"""Run the actual offline evaluation command and exercise publication boundaries."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_forecast_evaluation import CUTOFF, START, package

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/evaluate_forecast_candidate.py"


def inputs(tmp_path):
    data = tmp_path / "package.json"
    data.write_bytes(package(tmp_path))
    data.chmod(0o600)
    grants = tmp_path / "grants.json"
    grants.write_text(json.dumps([{"source_id": "recorded", "provider": "synthetic-test",
        "categories": ["MARKET", "BROKER"], "planes": ["TRAINING"], "point_in_time": True,
        "rights_status": "INTERNAL", "max_age_seconds": None}]))
    grants.chmod(0o600)
    return ["--package", str(data), "--grants", str(grants),
            "--output", str(tmp_path / "report.json"), "--training-cutoff", CUTOFF.isoformat(),
            "--holdout-start", START.isoformat(), "--run-id", "test", "--candidate-id", "candidate"]


def run(args, *, mode="false"):
    return subprocess.run([sys.executable, "-B", str(SCRIPT), *args],
        env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1",
             "TRADING_LIVE_MONEY_ACTIVE": mode}, capture_output=True, text=True, timeout=60, check=False)


def test_actual_cli_evaluates_and_retains_private_report_without_overwriting(tmp_path):
    args = inputs(tmp_path)
    source_bytes = (tmp_path / "package.json").read_bytes()
    completed = run(args)
    assert completed.returncode == 0, completed.stderr
    summary = json.loads(completed.stdout)
    assert summary["mode"] == "RETROSPECTIVE_HOLDOUT"
    assert summary["training_rows"] == summary["holdout_rows"] == 40
    assert summary["trading_authorized"] is summary["promotion_authorized"] is False
    assert str(tmp_path) not in completed.stdout + completed.stderr
    assert "INDIA:NSE:TEST" not in completed.stdout
    output = tmp_path / "report.json"
    assert output.stat().st_mode & 0o077 == 0
    report_bytes = output.read_bytes()
    report = json.loads(report_bytes)
    assert report["candidate"]["samples"] == 40
    assert len(report["predictions"]) == 40
    assert run(args).returncode == 2
    assert output.read_bytes() == report_bytes
    assert (tmp_path / "package.json").read_bytes() == source_bytes


@pytest.mark.parametrize("mode", ["true", "1", "FALSE"])
def test_cli_refuses_nonpaper_mode(tmp_path, mode):
    args = inputs(tmp_path)
    completed = run(args, mode=mode)
    assert completed.returncode == 2
    assert not (tmp_path / "report.json").exists()
    assert str(tmp_path) not in completed.stderr


@pytest.mark.parametrize("kind", ["missing", "public", "symlink", "malformed"])
def test_cli_refuses_invalid_inputs_without_partial_output_or_private_errors(tmp_path, kind):
    args = inputs(tmp_path)
    data = tmp_path / "package.json"
    if kind == "missing":
        data.unlink()
    elif kind == "public":
        data.chmod(0o644)
    elif kind == "symlink":
        data.rename(tmp_path / "original")
        data.symlink_to(tmp_path / "original")
    else:
        data.write_text('{"private_error_marker":')
    completed = run(args)
    assert completed.returncode == 2
    assert not (tmp_path / "report.json").exists()
    assert str(tmp_path) not in completed.stderr and "private_error_marker" not in completed.stderr


def test_cli_refuses_existing_output_before_reading_inputs(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("evaluate_forecast_candidate", SCRIPT)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    args = inputs(tmp_path)
    output = tmp_path / "report.json"
    output.write_text("preserve")
    monkeypatch.setenv("TRADING_LIVE_MONEY_ACTIVE", "false")
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), *args])
    def forbidden(*_args):
        pytest.fail("existing output must be refused before input reads")
    monkeypatch.setattr(cli, "read_private_bytes", forbidden)
    assert cli.main() == 2
    assert output.read_text() == "preserve"
