"""Boundary probes used both normally and by isolated mutation verification."""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_research_publisher import INSTRUMENT, NOW, producer_dataset, seeded_register, series

from quant_ai import cli
from quant_ai.backtesting import research_publisher as pub


@pytest.fixture(autouse=True)
def paper_only(monkeypatch):
    monkeypatch.setenv("TRADING_LIVE_MONEY_ACTIVE", "false")


def test_cli_dispatch_never_builds_a_trading_runtime(monkeypatch):
    received = []

    def capture(args):
        received.append(args)
        return 0

    def forbidden():
        pytest.fail("offline publisher must not assemble a trading runtime")

    monkeypatch.setattr(pub, "publish_from_args", capture)
    monkeypatch.setattr(cli, "build_runtime", forbidden)
    assert cli.main(["publish-research", "--data", "synthetic.json", "--market", "india"]) == 0
    assert len(received) == 1
    assert received[0].data == "synthetic.json"
    assert received[0].market == "india"


def test_short_write_cannot_replace_valid_report(tmp_path, monkeypatch):
    output = tmp_path / "report.json"
    output.write_text("previous valid report")
    original = pub.os.fdopen

    class PartialWriter:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def write(self, data):
            return self.stream.write(data[:len(data) // 2])

    monkeypatch.setattr(pub.os, "fdopen", lambda *a, **kw: PartialWriter(original(*a, **kw)))
    with pytest.raises(pub.ResearchPublicationRefused, match="research_partial_write"):
        pub._atomic_write(output, b"replacement document")
    assert output.read_text() == "previous valid report"
    assert not list(tmp_path.glob(".report.json.*"))


def test_duplicate_keys_refuse():
    with pytest.raises(pub.ResearchPublicationRefused, match="duplicate_json_key"):
        pub._unique_pairs([("adjusted_close_used", True), ("adjusted_close_used", False)])


def test_second_publisher_cannot_take_the_same_register_lock(tmp_path):
    bars = series()
    register = seeded_register(tmp_path, bars)
    with pub._exclusive_register(register):
        with pytest.raises(pub.ResearchPublicationRefused, match="already_running"):
            pub.publish_research(bars, instrument=INSTRUMENT, register=register,
                                 output=tmp_path / "report.json", now=NOW)


@pytest.mark.parametrize("case", ["adjusted", "identity", "interval", "market", "output"])
def test_invalid_input_refuses_before_any_publisher_call(tmp_path, monkeypatch, case):
    source = producer_dataset(tmp_path)
    payload = json.loads(source.read_text())
    market = "india"
    if case == "adjusted":
        payload["provenance"]["adjusted_close_used"] = True
    elif case == "identity":
        del payload["provenance"]["instrument"]
    elif case == "interval":
        payload["provenance"]["interval"] = "1m"
    elif case == "market":
        market = "us"
    source.write_text(json.dumps(payload))
    output = source if case == "output" else tmp_path / "report.json"
    monkeypatch.setenv("PRAMANA_RESEARCH_REPORT", str(output))
    calls = []

    def capture(*args, **kwargs):
        calls.append(1)
        return {"report_sha256": "a" * 64, "candidate_trials": 23}

    monkeypatch.setattr(pub, "publish_research", capture)
    args = SimpleNamespace(data=str(source), market=market, start=None, end=None)
    with pytest.raises(SystemExit, match="research publication refused"):
        pub.publish_from_args(args)
    assert not calls


def test_report_keeps_private_permissions(tmp_path):
    path = tmp_path / "report.json"
    pub._atomic_write(path, b"synthetic document")
    assert path.stat().st_mode & 0o777 == 0o600


def test_missing_register_does_not_create_fake_history(tmp_path):
    with pytest.raises(pub.ResearchPublicationRefused, match="existing_trial_register_required"):
        pub.publish_research(series(), instrument=INSTRUMENT, register=tmp_path / "absent.jsonl",
                             output=tmp_path / "report.json", now=NOW)
    assert not (tmp_path / "absent.jsonl").exists()


def test_register_change_during_evaluation_refuses(tmp_path, monkeypatch):
    bars = series()
    register = seeded_register(tmp_path, bars)
    original = pub.path_stress

    def changed(returns):
        result = original(returns)
        with register.open("ab") as handle:
            handle.write(b"\n")
        return result

    monkeypatch.setattr(pub, "path_stress", changed)
    with pytest.raises(pub.ResearchPublicationRefused, match="changed_during_publication"):
        pub.publish_research(bars, instrument=INSTRUMENT, register=register,
                             output=tmp_path / "report.json", now=NOW)
    assert not (tmp_path / "report.json").exists()


def test_default_output_uses_the_existing_ledger_directory(tmp_path, monkeypatch):
    source = producer_dataset(tmp_path)
    captured = {}
    monkeypatch.delenv("PRAMANA_RESEARCH_REPORT", raising=False)
    monkeypatch.setattr(pub.paths, "ledger_path", lambda *a: tmp_path / "paper.db")

    def capture(bars, **kwargs):
        captured.update(kwargs)
        return {"report_sha256": "a" * 64, "candidate_trials": 23}

    monkeypatch.setattr(pub, "publish_research", capture)
    args = SimpleNamespace(data=str(source), market=None, start=None, end=None)
    assert pub.publish_from_args(args) == 0
    assert captured["output"] == tmp_path / "research-report.json"
    assert not Path(tmp_path / "paper.db").exists()
    assert os.environ["TRADING_LIVE_MONEY_ACTIVE"] == "false"
