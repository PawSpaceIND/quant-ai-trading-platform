"""Security gate behavior with a controlled CLI fixture; no provider credentials."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("pramana_secret_scan", ROOT / "scripts/scan_repository_secrets.py")
assert SPEC is not None and SPEC.loader is not None
scan = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scan)


def finding():
    return {"File": "example.py", "StartLine": 3, "EndLine": 3,
            "RuleID": "generic-api-key", "Commit": "a" * 40, "Fingerprint": "fixture-location",
            "Secret": "private fixture contents", "Match": "private fixture match",
            "Description": "untrusted report description"}


def cli_fixture(tmp_path, phases):
    repo = tmp_path / "repo"
    repo.mkdir()
    config = tmp_path / "scanner-input.json"
    config.write_text(json.dumps(phases))
    calls = tmp_path / "calls.jsonl"
    cli = tmp_path / "scanner"
    cli.write_text(f"#!{sys.executable}\n" + "import json, pathlib, sys\n"
        + f"settings=json.loads(pathlib.Path({str(config)!r}).read_text())\n"
        + f"calls=pathlib.Path({str(calls)!r})\n"
        + "phase=sys.argv[1]\n"
        + "with calls.open('a') as handle: handle.write(json.dumps(sys.argv[1:])+'\\n')\n"
        + "chosen=settings[phase]\n"
        + "target=pathlib.Path(sys.argv[sys.argv.index('--report-path')+1])\n"
        + "if chosen.get('write', True): target.write_text(chosen.get('raw', json.dumps(chosen.get('rows', []))))\n"
        + "print('private scanner stdout should not appear in summary')\n"
        + "print('private scanner stderr should not appear in summary', file=sys.stderr)\n"
        + "raise SystemExit(chosen.get('code', 0))\n")
    cli.chmod(0o700)
    return cli, repo, tmp_path / "reports", calls


def test_both_scans_are_required_for_a_clean_result(tmp_path):
    cli, repo, output, calls = cli_fixture(tmp_path, {"git": {}, "dir": {}})
    report = scan.scan_repository(cli, repo, output)
    assert report["status"] == "clean"
    assert [p["phase"] for p in report["phases"]] == ["git", "dir"]
    commands = [json.loads(line) for line in calls.read_text().splitlines()]
    assert all("--redact=100" in cmd for cmd in commands)
    assert all("--log-opts" not in cmd and "--baseline-path" not in cmd for cmd in commands)
    assert (output / "secret-scan-summary.json").stat().st_mode & 0o777 == 0o600
    assert sorted(p.name for p in output.iterdir()) == ["secret-scan-summary.json"]


@pytest.mark.parametrize("phase", ["git", "dir"])
def test_any_finding_blocks_and_other_scan_still_runs(tmp_path, phase):
    phases = {"git": {}, "dir": {}}
    phases[phase] = {"code": 1, "rows": [finding()]}
    cli, repo, output, calls = cli_fixture(tmp_path, phases)
    report = scan.scan_repository(cli, repo, output)
    assert report["status"] == "failed"
    assert len(calls.read_text().splitlines()) == 2
    assert next(p for p in report["phases"] if p["phase"] == phase)["status"] == "findings"
    raw = (output / "secret-scan-summary.json").read_text()
    assert "private fixture" not in raw and "private scanner" not in raw
    assert "Secret" not in raw and "Match" not in raw and "Description" not in raw
    assert "example.py" in raw and "fixture-location" in raw


@pytest.mark.parametrize("failure", [
    {"code": 2, "write": False}, {"code": 0, "write": False},
    {"code": 0, "raw": "not-json"}, {"code": 0, "raw": "{}"},
    {"code": 0, "rows": [finding()]}, {"code": 1, "rows": []},
    {"code": 0, "rows": ["invalid"]},
    {"code": 1, "rows": [{**finding(), "StartLine": True}]},
    {"code": 1, "rows": [{**finding(), "File": None}]},
])
def test_errors_or_inconsistent_reports_cannot_become_success(tmp_path, failure):
    cli, repo, output, calls = cli_fixture(tmp_path, {"git": failure, "dir": {}})
    report = scan.scan_repository(cli, repo, output)
    assert report["status"] == "failed"
    assert report["phases"][0]["status"] == "error"
    assert report["phases"][1]["status"] == "clean"
    assert len(calls.read_text().splitlines()) == 2


def test_reports_cannot_contaminate_the_scanned_tree(tmp_path):
    cli, repo, _output, calls = cli_fixture(tmp_path, {"git": {}, "dir": {}})
    with pytest.raises(ValueError, match="outside_repository"):
        scan.scan_repository(cli, repo, repo / "reports")
    assert not calls.exists()


def test_scanner_timeouts_still_run_both_fail_closed_phases(tmp_path, monkeypatch):
    cli, repo, output, _calls = cli_fixture(tmp_path, {"git": {}, "dir": {}})
    attempted = []
    def timeout(command, **_kwargs):
        attempted.append(command[1])
        raise subprocess.TimeoutExpired(command, 300, output="private output")
    monkeypatch.setattr(scan.subprocess, "run", timeout)
    report = scan.scan_repository(cli, repo, output)
    assert report["status"] == "failed" and attempted == ["git", "dir"]
    assert "private output" not in json.dumps(report)


def test_only_one_exact_verified_fingerprint_is_exempted():
    lines = [line for line in (ROOT / ".gitleaksignore").read_text().splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    assert lines == ["916ff1681cacf1395476dfdbf41378eddd5a6bb0:docs/evidence/zerodha-token-renewal-sabotage.json:generic-api-key:264"]
    assert not (ROOT / ".gitleaks.toml").exists()


def test_ci_runs_risk_acceptance_without_success_override():
    text = (ROOT / ".github/workflows/ci.yml").read_text()
    job = text.split("  institutional-risk-acceptance:\n", 1)[1].split("\n  ui:", 1)[0]
    assert "acceptance/test_institutional_edge_authority.py" in job
    assert "PYTHONPATH: src:tests" in job
    assert 'TRADING_LIVE_MONEY_ACTIVE: "false"' in job
    assert "continue-on-error:" not in job and "|| true" not in job
    assert "--junitxml" in job and "actions/upload-artifact" in job


def test_ci_keeps_verified_download_and_full_scans_before_dependency_install():
    text = (ROOT / ".github/workflows/ci.yml").read_text().split("  security:\n", 1)[1]
    assert "fetch-depth: 0" in text and 'GITLEAKS_VERSION: "8.30.1"' in text
    assert text.index("sha256sum --check --strict") < text.index("scan_repository_secrets.py")
    assert text.index("scan_repository_secrets.py") < text.index("pip install")
    assert "secret-scan-location-summary" in text
    assert "continue-on-error:" not in text
    assert text.count("--ignore-vuln") == 2


def test_cli_cannot_succeed_without_a_working_scanner(tmp_path):
    result = subprocess.run([sys.executable, str(ROOT / "scripts/scan_repository_secrets.py"),
            "--scanner", str(tmp_path / "absent"), "--report-directory", str(tmp_path / "reports")],
            cwd=ROOT, capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode != 0 and json.loads(result.stdout)["status"] == "failed"
