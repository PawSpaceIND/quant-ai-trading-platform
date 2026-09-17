"""Exercise the real preflight CLI with synthetic inputs and no provider I/O."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cli(monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))
    spec = importlib.util.spec_from_file_location("preflight_publication", ROOT / "scripts/check_pilot_risk_gates.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "DailyHistoryProvider", lambda *_: object())
    return module


@pytest.fixture
def sample(tmp_path):
    paths = (tmp_path / "directives.json", tmp_path / "sectors.json")
    for path, name in zip(paths, ("founder-directives.example.json", "pilot-sector-map.example.json"), strict=True):
        path.write_bytes((ROOT / "deploy" / name).read_bytes())
    return paths


def args(sample, output=None):
    result = ["--online", "--directives", str(sample[0]), "--sector-map", str(sample[1])]
    return result + (["--output", str(output)] if output is not None else [])


def checker(monkeypatch, cli, callback=None, **extra):
    def checked(*inputs):
        if callback is not None:
            callback(*inputs)
        return {"dataReady": True, "paperOnly": True, "hostAcceptance": False, **extra}
    monkeypatch.setattr(cli, "check", checked)


def test_special_input_is_refused_without_reading_a_fifo(cli, tmp_path):
    path = tmp_path / "pipe"
    os.mkfifo(path)
    with pytest.raises(ValueError, match="must_be_regular"):
        cli._input_bytes(path)


@pytest.mark.parametrize("raw", [b"[]", b"null", b"true", b'"object-looking text"'])
def test_object_validation_has_a_consistent_contract(cli, raw):
    with pytest.raises(TypeError, match="must_be_object"):
        cli._input_object(raw)


def test_input_reader_accepts_exactly_the_budget_and_rejects_one_more(cli, tmp_path):
    path = tmp_path / "bounded"
    path.write_bytes(b" " * cli.MAX_INPUT_BYTES)
    assert len(cli._input_bytes(path)) == cli.MAX_INPUT_BYTES
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="too_large"):
        cli._input_bytes(path)


def test_directive_decimal_is_not_rounded_through_binary_float(cli, sample, monkeypatch):
    precise = "123456.123456789012345678901"
    text = sample[0].read_text().replace('"starting_capital": 100000', '"starting_capital": ' + precise)
    assert precise in text
    sample[0].write_text(text)
    observed = []
    checker(monkeypatch, cli, lambda directives, *_: observed.append(directives.starting_capital))
    assert cli.main(args(sample)) == 0
    assert observed == [Decimal(precise)]


@pytest.mark.parametrize("index", [0, 1])
def test_stdout_only_report_also_rechecks_both_input_files(cli, sample, monkeypatch, capsys, index):
    checker(monkeypatch, cli, lambda *_: sample[index].write_bytes(sample[index].read_bytes() + b" "))
    assert cli.main(args(sample)) == 2
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("index", [0, 1])
def test_inputs_changed_during_report_flush_refuse_publication(cli, sample, tmp_path, monkeypatch, capsys, index):
    checker(monkeypatch, cli)
    original = os.fsync
    def changed(descriptor):
        sample[index].write_bytes(sample[index].read_bytes() + b" ")
        original(descriptor)
    monkeypatch.setattr(os, "fsync", changed)
    output = tmp_path / "report.json"
    assert cli.main(args(sample, output)) == 2
    assert not output.exists() and capsys.readouterr().out == ""
    assert not list(tmp_path.glob(".risk-preflight-*"))


@pytest.mark.parametrize("kind", ["short", "incomplete_but_claimed_complete"])
def test_incomplete_report_cannot_reach_final_filename(cli, sample, tmp_path, monkeypatch, capsys, kind):
    checker(monkeypatch, cli)
    original = os.fdopen
    flushes = []
    sync = os.fsync
    def synced(fd):
        flushes.append(fd)
        sync(fd)
    monkeypatch.setattr(os, "fsync", synced)
    class Partial:
        def __init__(self, handle): self.handle = handle
        def __enter__(self): return self
        def __exit__(self, *_): self.handle.close()
        def __getattr__(self, key): return getattr(self.handle, key)
        def write(self, value):
            count = self.handle.write(value[:len(value) // 2])
            return count if kind == "short" else len(value)
    def opened(fd, mode, **kwargs):
        handle = original(fd, mode, **kwargs)
        return Partial(handle) if mode == "w+b" else handle
    monkeypatch.setattr(os, "fdopen", opened)
    output = tmp_path / "report.json"
    assert cli.main(args(sample, output)) == 2
    assert not output.exists() and capsys.readouterr().out == ""
    assert not list(tmp_path.glob(".risk-preflight-*"))
    if kind == "short":
        assert flushes == []


def test_publication_race_keeps_another_writers_complete_file(cli, sample, tmp_path, monkeypatch, capsys):
    checker(monkeypatch, cli)
    output = tmp_path / "report.json"
    original = os.fsync
    def race(fd):
        output.write_text("other writer")
        original(fd)
    monkeypatch.setattr(os, "fsync", race)
    assert cli.main(args(sample, output)) == 2
    assert output.read_text() == "other writer"
    assert capsys.readouterr().out == ""


def test_directory_flush_failure_is_nonzero_with_only_a_complete_report(cli, sample, tmp_path, monkeypatch, capsys):
    checker(monkeypatch, cli)
    original = os.fsync
    count = []
    def fail_directory(fd):
        count.append(fd)
        if len(count) == 2:
            raise OSError("synthetic directory sync failure")
        original(fd)
    monkeypatch.setattr(os, "fsync", fail_directory)
    output = tmp_path / "report.json"
    assert cli.main(args(sample, output)) == 2
    assert json.loads(output.read_text())["inputs"]["unchangedAtCompletion"] is True
    assert output.stat().st_mode & 0o777 == 0o600
    assert capsys.readouterr().out == ""
    assert not list(tmp_path.glob(".risk-preflight-*"))


def test_staging_failure_happens_before_provider_construction(cli, sample, tmp_path, monkeypatch):
    calls = []
    def unwritable(**_): raise PermissionError("synthetic denied output")
    monkeypatch.setattr(cli.tempfile, "mkstemp", unwritable)
    monkeypatch.setattr(cli, "DailyHistoryProvider", lambda *_: calls.append("provider"))
    assert cli.main(args(sample, tmp_path / "report.json")) == 2
    assert calls == []


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_report_cannot_be_printed_or_published(cli, sample, tmp_path, monkeypatch, capsys, value):
    checker(monkeypatch, cli, broken=value)
    output = tmp_path / "report.json"
    assert cli.main(args(sample, output)) == 2
    assert not output.exists() and capsys.readouterr().out == ""
    assert not list(tmp_path.glob(".risk-preflight-*"))


def test_invalid_private_input_text_is_not_echoed(cli, sample, monkeypatch, capsys):
    marker = "private-config-marker"
    sample[0].write_text('{"instructions": "' + marker + '" BROKEN}')
    checker(monkeypatch, cli)
    assert cli.main(args(sample)) == 2
    output = capsys.readouterr()
    assert output.out == "" and marker not in output.err
