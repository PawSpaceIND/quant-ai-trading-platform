"""Paired guard-removal checks in disposable copies, with synthetic inputs only."""
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
CASES = json.loads((ROOT / "docs/evidence/consensus-output-guard-inventory.json").read_text())["mutations"]
PROTECTED = sorted({c["path"] for c in CASES} | {c["test"].split("::")[0] for c in CASES})
LAUNCHER = """
import sys

def deny_network(event, args):
    if event in ('socket.connect', 'socket.connect_ex', 'socket.getaddrinfo', 'socket.sendto'):
        raise RuntimeError('offline_consensus_verification_network_forbidden')

sys.addaudithook(deny_network)
import pytest
raise SystemExit(pytest.main(sys.argv[1:]))
"""


def hashes():
    return {s: hashlib.sha256((ROOT / s).read_bytes()).hexdigest() for s in PROTECTED}


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_consensus_guard_has_passing_control_and_detected_change(tmp_path, case):
    before = hashes()
    source = (ROOT / case["path"]).read_text()
    assert source.count(case["old"]) == 1, case["id"]
    work = tmp_path / "copy"
    work.mkdir()
    for directory in ("src", "tests", "scripts", "deploy"):
        shutil.copytree(ROOT / directory, work / directory,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "node_modules"))
    shutil.copyfile(ROOT / "pyproject.toml", work / "pyproject.toml")
    home = tmp_path / "home"
    home.mkdir()
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "TMPDIR": str(tmp_path),
           "PYTHONPATH": str(work / "src"), "PYTHONDONTWRITEBYTECODE": "1",
           "TRADING_LIVE_MONEY_ACTIVE": "false"}

    def run(label):
        xml = tmp_path / (label + ".xml")
        result = subprocess.run([sys.executable, "-B", "-c", LAUNCHER, "-q", case["test"],
            "--basetemp=" + str(tmp_path / (label + "-tmp")), "--junitxml=" + str(xml)],
            cwd=work, env=env, capture_output=True, text=True, timeout=60, check=False)
        (tmp_path / (label + ".log")).write_text(result.stdout + result.stderr)
        assert xml.exists(), case["id"]
        suites = list(ET.parse(xml).iter("testsuite"))
        counts = {key: sum(int(s.get(key, 0)) for s in suites)
                  for key in ("tests", "failures", "errors", "skipped")}
        return result, counts

    target = work / case["path"]
    outcome = {"id": case["id"], "test": case["test"], "certified": False}
    try:
        result, good = run("control")
        outcome["control"] = {"exit": result.returncode, **good}
        assert result.returncode == 0 and good["tests"] > 0
        assert good["failures"] == good["errors"] == good["skipped"] == 0
        target.write_text(source.replace(case["old"], case["new"], 1))
        result, bad = run("changed")
        outcome["changed"] = {"exit": result.returncode, **bad}
        assert result.returncode == 1 and bad["failures"] > 0
        assert bad["errors"] == bad["skipped"] == 0
        outcome["certified"] = True
    finally:
        target.write_text(source)
        outcome["copy_restored"] = target.read_text() == source
        outcome["original_unchanged"] = hashes() == before
        (tmp_path / "result.json").write_text(json.dumps(outcome, indent=2) + "\n")
        assert outcome["copy_restored"] and outcome["original_unchanged"]
        shutil.rmtree(work)
