"""Synthetic concurrency probes; these are not observed market results.

Legacy research commands do not acquire the publisher lock. A valid append after
holdout screening must not be adopted as the publisher's trusted starting state.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from test_research_publisher import INSTRUMENT, NOW, STUDY, seeded_register, series

from quant_ai.backtesting import research_publisher as pub
from quant_ai.backtesting.baselines import BaselineEvaluator
from quant_ai.operations.evidence_log import read_records, verify_chain
from quant_ai.validation.trial_register import record_trials, register_summary

ROOT = Path(__file__).resolve().parents[1]
MODULE = Path("src/quant_ai/backtesting/research_publisher.py")
THIS_TEST = "tests/test_research_publisher_reservation.py"


@pytest.fixture(autouse=True)
def paper_only(monkeypatch):
    monkeypatch.setenv("TRADING_LIVE_MONEY_ACTIVE", "false")


@pytest.mark.parametrize("stage", ["before", "after"])
def test_concurrent_reservation_preserves_report(tmp_path, monkeypatch, stage):
    bars = series()
    register = seeded_register(tmp_path, bars)
    output = tmp_path / "report.json"
    previous = b"previous report must survive; synthetic sentinel"
    output.write_bytes(previous)
    reserve = pub.record_trials
    evaluations = []
    original_run = BaselineEvaluator.run

    def evaluate(self, *args, **kwargs):
        evaluations.append(1)
        return original_run(self, *args, **kwargs)

    def competing_writer():
        # This is a real, valid hash-chained append, not corrupt JSON or a mock count.
        record_trials(
            register, study=STUDY, candidate_trials=1,
            configuration={"command": "synthetic-legacy-contest",
                           "start": bars[840].timestamp.isoformat(),
                           "end": bars[-1].timestamp.isoformat()},
            data_sha256=pub.bar_digest(bars[840:]), now=NOW,
        )

    def interleaved_reservation(*args, **kwargs):
        if stage == "before":
            competing_writer()
        reserved = reserve(*args, **kwargs)
        if stage == "after":
            competing_writer()
        return reserved

    monkeypatch.setattr(pub, "record_trials", interleaved_reservation)
    monkeypatch.setattr(BaselineEvaluator, "run", evaluate)
    with pytest.raises(pub.ResearchPublicationRefused, match="trial_register_changed_during_read"):
        pub.publish_research(
            bars, instrument=INSTRUMENT, register=register, output=output, now=NOW,
            source_description="synthetic reservation race; not market evidence",
        )
    assert output.read_bytes() == previous
    assert evaluations == [], "Refusal must happen before any training or holdout evaluation"
    # No rollback erases either writer's evidence. Reserved but unscored candidates
    # remain counted conservatively, consistent with other publisher refusals.
    assert register_summary(register, study=STUDY)["candidate_trials"] == 24
    assert verify_chain(read_records(register))


def test_single_writer_appends_once_and_pins_the_exact_register(tmp_path):
    bars = series()
    register = seeded_register(tmp_path, bars)
    original = register.read_bytes()
    result = pub.publish_research(
        bars, instrument=INSTRUMENT, register=register,
        output=tmp_path / "report.json", now=NOW,
        source_description="synthetic single-writer control; not market evidence",
    )
    stored = register.read_bytes()
    records = read_records(register)
    assert stored == original + pub._canonical(records[-1]) + b"\n"
    assert len(records) == 21
    assert result["candidate_trials"] == 23
    assert result["trial_register"]["snapshot_sha256"] == hashlib.sha256(stored).hexdigest()
    assert verify_chain(records)


@pytest.mark.parametrize("stage", ["before", "after"])
def test_expected_append_sabotage_is_caught_and_restored(tmp_path, stage):
    original = (ROOT / MODULE).read_bytes()
    isolated = tmp_path / "isolated"
    shutil.copytree(ROOT / "src", isolated / "src",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (isolated / "tests").mkdir()
    for name in ("test_research_publisher.py", Path(THIS_TEST).name):
        shutil.copy2(ROOT / "tests" / name, isolated / "tests" / name)
    environment = {**os.environ, "PYTHONPATH": str(isolated / "src"),
                   "PYTHONDONTWRITEBYTECODE": "1", "TRADING_LIVE_MONEY_ACTIVE": "false"}
    environment.pop("PYTEST_ADDOPTS", None)
    nodeid = f"{THIS_TEST}::test_concurrent_reservation_preserves_report[{stage}]"

    def execute(label):
        evidence = tmp_path / f"{label}.xml"
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", nodeid, f"--junitxml={evidence}"],
            cwd=isolated, env=environment, capture_output=True, text=True,
            timeout=120, check=False,
        )
        (tmp_path / f"{label}.log").write_text(result.stdout + result.stderr)
        assert evidence.is_file(), result.stdout + result.stderr
        suites = list(ET.parse(evidence).getroot().iter("testsuite"))
        counts = {key: sum(int(s.get(key, "0")) for s in suites)
                  for key in ("tests", "failures", "errors", "skipped")}
        assert counts["tests"] == 1 and counts["errors"] == counts["skipped"] == 0
        return result, counts["failures"]

    try:
        control, failures = execute("control")
        assert control.returncode == 0 and failures == 0, control.stdout + control.stderr
        text = original.decode()
        needle = "and (expected_content is None or original == expected_content)"
        assert text.count(needle) == 1, "Expected-append condition moved; update sabotage deliberately"
        # Remove only the new expected-append predicate. The existing stable-read
        # and final-publication drift guards remain intact, reproducing the old gap.
        mutated = text.replace(needle, "", 1)
        compile(mutated, str(MODULE), "exec")
        (isolated / MODULE).write_text(mutated)
        mutant, failures = execute("mutant")
        assert mutant.returncode == 1 and failures == 1, mutant.stdout + mutant.stderr
        assert "DID NOT RAISE" in mutant.stdout, "The race must publish, not fail for an unrelated error"
    finally:
        (isolated / MODULE).write_bytes(original)
        assert (ROOT / MODULE).read_bytes() == original
        assert (isolated / MODULE).read_bytes() == original
