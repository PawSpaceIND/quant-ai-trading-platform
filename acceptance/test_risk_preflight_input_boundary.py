"""Preflight file/input boundaries only; no broker, live feed or model requests."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cli(monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))
    spec = importlib.util.spec_from_file_location("preflight_boundary", ROOT / "scripts/check_pilot_risk_gates.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def inputs(tmp_path):
    directives = tmp_path / "directives.json"
    sectors = tmp_path / "sectors.json"
    directives.write_bytes((ROOT / "deploy/founder-directives.example.json").read_bytes())
    sectors.write_bytes((ROOT / "deploy/pilot-sector-map.example.json").read_bytes())
    return directives, sectors


def arguments(inputs, output=None, online=True):
    args = ["--directives", str(inputs[0]), "--sector-map", str(inputs[1])]
    if online:
        args.append("--online")
    if output is not None:
        args += ["--output", str(output)]
    return args


def fake_check(monkeypatch, cli, callback=None, ready=True):
    calls = []
    monkeypatch.setattr(cli, "DailyHistoryProvider", lambda *_a: object())
    def run(*_args):
        calls.append("check")
        if callback:
            callback()
        return {"schema": "pramana.pilot_risk_preflight.v1", "allArmed": True,
                "dataReady": ready, "hostAcceptance": False, "paperOnly": True}
    monkeypatch.setattr(cli, "check", run)
    return calls


@pytest.mark.parametrize("fragment", [
    '"max_open_positions":5,"max_open_positions":5',
    '"extra":{"field":"first","field":"last"}',
    '"extra":{"a":1,"\\u0061":2}',
])
def test_duplicate_directive_keys_refuse_before_history(cli, inputs, monkeypatch, capsys, fragment):
    inputs[0].write_text(inputs[0].read_text().rstrip()[:-1] + "," + fragment + "}")
    calls = fake_check(monkeypatch, cli)
    assert cli.main(arguments(inputs)) == 2
    assert calls == []
    assert '"dataReady": true' not in capsys.readouterr().out


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_non_json_numbers_refuse_before_history(cli, inputs, monkeypatch, value):
    inputs[0].write_text(inputs[0].read_text().rstrip()[:-1] + ',"extra":' + value + "}")
    calls = fake_check(monkeypatch, cli)
    assert cli.main(arguments(inputs)) == 2
    assert calls == []


@pytest.mark.parametrize("which", [0, 1])
def test_top_level_must_be_object(cli, inputs, monkeypatch, which):
    inputs[which].write_text("[]")
    calls = fake_check(monkeypatch, cli)
    assert cli.main(arguments(inputs)) == 2
    assert calls == []


@pytest.mark.parametrize("which", [0, 1])
def test_oversized_inputs_refuse_before_history(cli, inputs, monkeypatch, which):
    inputs[which].write_bytes(b" " * (1024 * 1024 + 1) + inputs[which].read_bytes())
    calls = fake_check(monkeypatch, cli)
    assert cli.main(arguments(inputs)) == 2
    assert calls == []


@pytest.mark.parametrize("kind", ["file", "directory", "dangling_symlink", "missing_parent"])
def test_unusable_output_refuses_before_history(cli, inputs, tmp_path, monkeypatch, kind):
    output = tmp_path / "report.json"
    if kind == "file":
        output.write_text("preserve")
    elif kind == "directory":
        output.mkdir()
    elif kind == "dangling_symlink":
        output.symlink_to(tmp_path / "absent-target")
    else:
        output = tmp_path / "absent-parent/report.json"
    calls = fake_check(monkeypatch, cli)
    assert cli.main(arguments(inputs, output)) == 2
    assert calls == []
    if kind == "file":
        assert output.read_text() == "preserve"


@pytest.mark.parametrize("which", [0, 1])
def test_input_changed_during_check_cannot_publish_success(cli, inputs, tmp_path, monkeypatch, capsys, which):
    output = tmp_path / "report.json"
    def changed():
        inputs[which].write_bytes(inputs[which].read_bytes() + b" ")
    fake_check(monkeypatch, cli, changed)
    assert cli.main(arguments(inputs, output)) == 2
    assert not output.exists()
    assert '"dataReady": true' not in capsys.readouterr().out


def test_reports_bind_exact_input_bytes_without_copying_them(cli, inputs, tmp_path, monkeypatch, capsys):
    original = [path.read_bytes() for path in inputs]
    calls = fake_check(monkeypatch, cli)
    output = tmp_path / "report.json"
    assert cli.main(arguments(inputs, output)) == 0
    result = json.loads(output.read_text())
    assert calls == ["check"]
    assert result["inputs"] == {
        "directivesSha256": hashlib.sha256(original[0]).hexdigest(),
        "sectorMapSha256": hashlib.sha256(original[1]).hexdigest(),
        "unchangedAtCompletion": True,
    }
    assert json.loads(capsys.readouterr().out) == result
    assert result["hostAcceptance"] is False
    assert "starting_capital" not in output.read_text()
    assert [path.read_bytes() for path in inputs] == original


def test_write_failure_never_publishes_a_partial_report(cli, inputs, tmp_path, monkeypatch, capsys):
    fake_check(monkeypatch, cli)
    def failure(*_args):
        raise OSError("synthetic flush failure")
    monkeypatch.setattr(os, "fsync", failure)
    output = tmp_path / "report.json"
    assert cli.main(arguments(inputs, output)) == 2
    assert not output.exists()
    assert not list(tmp_path.glob(".risk-preflight-*"))
    assert capsys.readouterr().out == ""


def test_published_report_has_private_permissions(cli, inputs, tmp_path, monkeypatch):
    fake_check(monkeypatch, cli)
    output = tmp_path / "report.json"
    old = os.umask(0)
    try:
        assert cli.main(arguments(inputs, output)) == 0
    finally:
        os.umask(old)
    assert output.stat().st_mode & 0o777 == 0o600


def test_a_concurrent_report_is_not_replaced(cli, inputs, tmp_path, monkeypatch, capsys):
    output = tmp_path / "report.json"
    fake_check(monkeypatch, cli, lambda: output.write_text("other writer"))
    assert cli.main(arguments(inputs, output)) == 2
    assert output.read_text() == "other writer"
    assert not list(tmp_path.glob(".risk-preflight-*"))
    assert capsys.readouterr().out == ""


def test_no_online_authority_means_no_provider_or_output(cli, inputs, tmp_path, monkeypatch):
    calls = fake_check(monkeypatch, cli)
    output = tmp_path / "report.json"
    assert cli.main(arguments(inputs, output, online=False)) == 2
    assert calls == [] and not output.exists()


def test_unready_result_remains_nonzero_with_bound_inputs(cli, inputs, tmp_path, monkeypatch):
    fake_check(monkeypatch, cli, ready=False)
    output = tmp_path / "report.json"
    assert cli.main(arguments(inputs, output)) == 1
    result = json.loads(output.read_text())
    assert result["dataReady"] is False and result["hostAcceptance"] is False
    assert result["inputs"]["unchangedAtCompletion"] is True
