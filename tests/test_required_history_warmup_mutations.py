"""Offline mutation checks: copied source only, fake data, no network or credentials."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "docs/evidence/required-history-warmup-guard-inventory.json"
CASES = json.loads(INVENTORY.read_text())["mutations"]
PROTECTED = tuple(sorted({row["path"] for row in CASES} | {row["test"].split("::")[0] for row in CASES}))
LAUNCHER = """
import sys

def deny_network(event, args):
    if event in ('socket.connect', 'socket.connect_ex', 'socket.getaddrinfo', 'socket.sendto'):
        raise RuntimeError('offline_warmup_verification_network_forbidden')

sys.addaudithook(deny_network)
import pytest
raise SystemExit(pytest.main(sys.argv[1:]))
"""


def hashes():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in PROTECTED}


@pytest.mark.parametrize("case", CASES, ids=[row["id"] for row in CASES])
def test_required_warmup_guard_requires_passing_control_and_failing_mutant(tmp_path, case):
    before = hashes()
    source = (ROOT / case["path"]).read_text()
    assert source.count(case["old"]) == 1, f"Review changed anchor: {case['id']}"
    work = tmp_path / "copy"
    work.mkdir()
    for directory in ("src", "tests", "deploy", ".github"):
        shutil.copytree(ROOT / directory, work / directory,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "node_modules"))
    shutil.copyfile(ROOT / "pyproject.toml", work / "pyproject.toml")
    home = tmp_path / "home"
    home.mkdir()
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "TMPDIR": str(temporary),
           "PYTHONPATH": str(work / "src"), "PYTHONDONTWRITEBYTECODE": "1",
           "TRADING_LIVE_MONEY_ACTIVE": "false"}

    def run(label):
        xml = tmp_path / (label + ".xml")
        result = subprocess.run(
            [sys.executable, "-B", "-c", LAUNCHER, "-q", case["test"],
             "--basetemp=" + str(tmp_path / (label + "-state")), "--junitxml=" + str(xml)],
            cwd=work, env=env, capture_output=True, text=True, timeout=60, check=False)
        (tmp_path / (label + ".log")).write_text(result.stdout + result.stderr)
        assert xml.is_file(), f"Missing {label} result: {case['id']}"
        tree = ET.parse(xml)
        suites = list(tree.iter("testsuite"))
        counts = {key: sum(int(suite.get(key, 0)) for suite in suites)
                  for key in ("tests", "failures", "errors", "skipped")}
        return result, counts

    target = work / case["path"]
    outcome = {"id": case["id"], "path": case["path"], "test": case["test"], "certified": False}
    try:
        control, good = run("control")
        outcome["control"] = {"exit": control.returncode, **good}
        assert control.returncode == 0 and good["tests"] > 0, control.stdout + control.stderr
        assert good["failures"] == good["errors"] == good["skipped"] == 0
        target.write_text(source.replace(case["old"], case["new"], 1))
        mutant, broken = run("mutant")
        outcome["mutant"] = {"exit": mutant.returncode, **broken}
        assert mutant.returncode == 1, mutant.stdout + mutant.stderr
        assert broken["failures"] > 0 and broken["errors"] == broken["skipped"] == 0
        outcome["certified"] = True
    finally:
        target.write_text(source)
        outcome["copy_restored"] = target.read_text() == source
        outcome["checkout_unchanged"] = hashes() == before
        (tmp_path / "result.json").write_text(json.dumps(outcome, indent=2) + "\n")
        assert outcome["copy_restored"] and outcome["checkout_unchanged"]
        shutil.rmtree(work)
