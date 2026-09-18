"""Run both pinned Gitleaks scans and emit only bounded finding-location metadata.

The caller verifies the scanner binary before invocation. Raw scanner reports are
redacted, temporary, and never printed or included in the published summary.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

SAFE_FIELDS = ("File", "StartLine", "EndLine", "RuleID", "Commit", "Fingerprint")


def _finding_metadata(row: object) -> dict:
    if not isinstance(row, dict):
        raise TypeError("invalid_finding_record")
    result = {}
    for key in SAFE_FIELDS:
        value = row.get(key)
        if key in {"StartLine", "EndLine"}:
            if type(value) is not int or value < 0:
                raise ValueError("invalid_finding_location")
        elif not isinstance(value, str) or len(value) > 1024:
            raise ValueError("invalid_finding_metadata")
        result[key] = value
    return result


def _scan(scanner: Path, repository: Path, report: Path, phase: str) -> dict:
    command = [str(scanner), phase, str(repository), "--redact=100", "--no-banner",
               "--log-level", "error", "--report-format", "json", "--report-path", str(report)]
    result = {"phase": phase, "status": "error", "exit_code": None, "findings": []}
    try:
        process = subprocess.run(command, cwd=repository, capture_output=True,
                                 text=True, timeout=300, check=False)
        result["exit_code"] = process.returncode
        rows = json.loads(report.read_text(encoding="utf-8"))
        if not isinstance(rows, list):
            raise TypeError("invalid_scanner_report")
        result["findings"] = [_finding_metadata(row) for row in rows]
        if process.returncode == 0 and not rows:
            result["status"] = "clean"
        elif process.returncode == 1 and rows:
            result["status"] = "findings"
        else:
            result["error"] = "scanner_status_report_disagreement"
    except (OSError, ValueError, TypeError, UnicodeError, subprocess.TimeoutExpired) as error:
        # Never echo scanner output or exceptions that may contain a matched secret.
        result["error"] = type(error).__name__
    return result


def scan_repository(scanner: Path, repository: Path, report_directory: Path) -> dict:
    repository = repository.resolve(strict=True)
    scanner = scanner.resolve(strict=True)
    report_directory = report_directory.resolve()
    if report_directory == repository or repository in report_directory.parents:
        raise ValueError("scan_reports_must_be_outside_repository")
    report_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix="redacted-", dir=report_directory) as directory:
        phases = [_scan(scanner, repository, Path(directory) / f"{phase}.json", phase)
                  for phase in ("git", "dir")]
    summary = {"schema": "pramana.secret_scan.v1",
               "status": "clean" if all(p["status"] == "clean" for p in phases) else "failed",
               "phases": phases}
    target = report_directory / "secret-scan-summary.json"
    if target.is_symlink():
        raise ValueError("scan_summary_symlink_unsupported")
    target.write_text(json.dumps(summary, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    target.chmod(0o600)
    return summary


def verify_scanner_detection(scanner: Path, repository: Path) -> None:
    """An ignore entry must not exempt another commit of the same evidence path.

    The generated value is a SHA-256 of a public test phrase, not a credential.
    It intentionally triggers the same generic rule in an isolated temporary repo.
    """
    with tempfile.TemporaryDirectory(prefix="pramana-scanner-sentinel-") as directory:
        root = Path(directory)
        ignore = repository / ".gitleaksignore"
        if ignore.exists():
            (root / ".gitleaksignore").write_bytes(ignore.read_bytes())
        relative = Path("docs/evidence/zerodha-token-renewal-sabotage.json")
        evidence = root / relative
        evidence.parent.mkdir(parents=True)
        digest = hashlib.sha256(b"public scanner self-test: never an account credential").hexdigest()
        evidence.write_text(json.dumps({"restored_sha256": {"scripts/renew_pilot_token.py": digest}}))
        for args in (["init", "-q"], ["add", "."], ["-c", "user.name=Scanner Test",
                "-c", "user.email=scanner@example.invalid", "commit", "-qm", "synthetic scanner sentinel"]):
            subprocess.run(["git", *args], cwd=root, capture_output=True, check=True, timeout=30)
        # Both history and live-tree scans must still find the synthetic sentinel.
        for phase in ("git", "dir"):
            report = root.parent / (root.name + f"-{phase}.json")
            try:
                result = _scan(scanner.resolve(), root, report, phase)
                if result["status"] != "findings" or not any(
                    r["File"].endswith(str(relative)) and r["RuleID"] == "generic-api-key"
                    for r in result["findings"]
                ):
                    raise ValueError("scanner_sentinel_not_detected")
            finally:
                report.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scanner", type=Path, required=True)
    parser.add_argument("--repository", type=Path, default=Path("."))
    parser.add_argument("--report-directory", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        verify_scanner_detection(args.scanner, args.repository)
        result = scan_repository(args.scanner, args.repository, args.report_directory)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
        print(json.dumps({"status": "failed", "error": type(error).__name__}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "clean" else 1


if __name__ == "__main__":
    raise SystemExit(main())
