"""Security gate behavior with a controlled CLI fixture; no provider credentials."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("pramana_secret_scan", ROOT / "scripts/scan_repository_secrets.py")
assert SPEC is not None and SPEC.loader is not None
scan = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scan)


@pytest.fixture(autouse=True)
def restore_process_umask():
    previous = os.umask(0o077)
    os.umask(previous)
    try:
        yield
    finally:
        os.umask(previous)


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


def test_reformatted_evidence_reconstructs_the_entire_original_document():
    report = json.loads((ROOT / "docs/evidence/zerodha-token-renewal-sabotage.json").read_text())
    expected = report.pop("legacy_document_sha256")
    records = report.pop("restored_files")
    assert len(records) == 5
    assert all(set(row) == {"path", "sha256"} for row in records)
    assert len({row["path"] for row in records}) == len(records)
    report["restored_sha256"] = {row["path"]: row["sha256"] for row in records}
    assert hashlib.sha256((json.dumps(report, indent=2) + "\n").encode()).hexdigest() == expected
    assert len(report["mutations"]) == 32 and report["restored"] is True
    assert all(row["caught"] and row["collection_errors"] == 0 for row in report["mutations"])


@pytest.mark.parametrize("phase", ["git", "dir"])
@pytest.mark.parametrize("bad", [
    {"status": "clean", "findings": []},
    {"status": "error", "findings": []},
    {"status": "findings", "findings": [{"File": "other.py", "RuleID": "generic-api-key"}]},
])
def test_actual_scanner_self_test_cannot_accept_a_missing_same_path_detection(tmp_path, monkeypatch, phase, bad):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".gitleaksignore").write_bytes((ROOT / ".gitleaksignore").read_bytes())
    def controlled(_scanner, _root, _report, mode):
        if mode == phase:
            return bad
        return {"status": "findings", "findings": [{
            "File": "docs/evidence/zerodha-token-renewal-sabotage.json", "RuleID": "generic-api-key"}]}
    monkeypatch.setattr(scan, "_scan", controlled)
    with pytest.raises(ValueError, match="sentinel_not_detected"):
        scan.verify_scanner_detection(tmp_path / "scanner", repo)


def test_cli_self_test_failure_cannot_reach_repository_scan(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["scan", "--scanner", str(tmp_path / "scanner"),
                                     "--report-directory", str(tmp_path / "reports")])
    def deny(*_args):
        raise ValueError("private diagnostic must stay private")
    def forbidden(*_args):
        pytest.fail("Repository scan followed a failed self-test")
    monkeypatch.setattr(scan, "verify_scanner_detection", deny)
    monkeypatch.setattr(scan, "scan_repository", forbidden)
    assert scan.main() == 2
    output = capsys.readouterr()
    assert "private diagnostic" not in output.out + output.err
    assert json.loads(output.out)["status"] == "failed"


@pytest.mark.parametrize("status,exit_code", [("clean", 0), ("failed", 1)])
def test_cli_exit_status_tracks_both_scan_result(tmp_path, monkeypatch, capsys, status, exit_code):
    monkeypatch.setattr(sys, "argv", ["scan", "--scanner", str(tmp_path / "scanner"),
                                     "--report-directory", str(tmp_path / "reports")])
    steps = []
    monkeypatch.setattr(scan, "verify_scanner_detection", lambda *_args: steps.append("self_test"))
    def completed(*_args):
        steps.append("repository")
        return {"status": status}
    monkeypatch.setattr(scan, "scan_repository", completed)
    assert scan.main() == exit_code
    assert steps == ["self_test", "repository"]
    assert json.loads(capsys.readouterr().out)["status"] == status


def test_summary_symlink_cannot_overwrite_an_unrelated_file(tmp_path):
    cli, repo, output, _calls = cli_fixture(tmp_path, {"git": {}, "dir": {}})
    output.mkdir()
    unrelated = tmp_path / "keep.txt"
    unrelated.write_text("preserve")
    (output / "secret-scan-summary.json").symlink_to(unrelated)
    with pytest.raises(ValueError, match="symlink"):
        scan.scan_repository(cli, repo, output)
    assert unrelated.read_text() == "preserve"


@pytest.mark.parametrize("field,value", [("StartLine", -1), ("EndLine", "3"), ("RuleID", "x" * 1025)])
def test_malformed_metadata_is_not_published_as_a_success(tmp_path, field, value):
    cli, repo, output, _calls = cli_fixture(tmp_path, {
        "git": {"code": 1, "rows": [{**finding(), field: value}]}, "dir": {}})
    result = scan.scan_repository(cli, repo, output)
    assert result["status"] == "failed"
    assert result["phases"][0]["status"] == "error"
    assert result["phases"][0]["findings"] == []
