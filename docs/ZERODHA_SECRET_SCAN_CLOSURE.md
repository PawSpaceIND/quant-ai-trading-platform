# Zerodha renewal: verified secret-scan false positive and closure evidence

## Scope and provenance

This is a selective integration into PR #129 from the independently published
scanner repair in PR #126, commit `050a16ae8a87882f14cc477043d7a13f962d6628`.
The scanner helper is copied byte-for-byte. Only the security workflow section and
applicable scanner tests are included. No institutional coordinator/risk code or
risk-acceptance job is brought into this branch. All five pre-existing non-security
CI jobs are unchanged; PR #126 and other worktrees are untouched.

## Finding identified, not assumed away

The prior run at `a6cc723...` had five successful application/build jobs and failed
Gitleaks at the full-history scan. Its log reported one finding without a location.
Parallel scanner work identified the exact historical fingerprint as:

`916ff1681cacf1395476dfdbf41378eddd5a6bb0:docs/evidence/zerodha-token-renewal-sabotage.json:generic-api-key:264`

It refers to the recorded SHA-256 checksum of `scripts/renew_pilot_token.py`, not an
account token. This continuation independently retrieved the script's bytes and
the evidence JSON from that exact Git commit and verified checksum equality without
reading any production environment or account credentials.

`.gitleaksignore` contains only this immutable historical commit/path/rule/line
fingerprint. It excludes no file, rule, branch, digest pattern, or history range.
Remove it when the historical finding is no longer in the scanned graph; a different
fingerprint requires investigation, not automatic exemption.

## Current-tree evidence is preserved, not waived

This branch still contains the original evidence document, unlike the donor
integration branch. The five path/checksum pairs are therefore represented as
`restored_files` records with distinct `path` and `sha256` fields, rather than a
credential-looking assignment whose key contains "token". No finding exception is
added for the current file. All 32 original mutation results and all five checksums
are retained. `legacy_document_sha256` plus a regression reconstruct and verify the
entire original JSON document byte-for-byte, including ordering and formatting.
This changes evidence serialization only, not any historical test outcome.

## Scanner remains blocking

The existing pinned Gitleaks version and archive checksum verification remain
unchanged. The imported helper runs both full Git history and working-tree scans,
even after the first phase reports findings or errors. Reports are fully redacted,
temporary, and removed after extracting bounded location metadata. Secret values,
matched text, descriptions and scanner stdout/stderr are not published.

Before scanning the repository, the real scanner must detect a generated public
noncredential test value at the same evidence path in a different commit, in both
Git and directory modes. Missing detection prevents success. This is a regression
check of the exception's scope, not a fake account credential or broker request.

Findings, nonzero scanner errors, timeouts, malformed/missing reports, status/report
disagreement and invalid metadata all block success. The original two documented
Python dependency exceptions are unchanged; no new dependency exception is added.

## New local verification

Baseline: **13 failed, 1640 passed, 2 warnings, 14 subtests passed in 52.09s**.

Candidate: **13 failed, 1671 passed, 2 warnings, 14 subtests passed in 65.79s (0:01:05)**.

The 13 failures are exactly the same existing Mac deployment-portability tests,
compared by JUnit identities. No skips or waived new failures. The 194-case focused
suite passed, including 31 added scanner/evidence cases. Ruff and whitespace checks
passed. The absolute existing project virtual-environment interpreter is used;
`/usr/local/bin/ruff` is absent on this Mac.

All 458 selected source/test/script/config files are hash-identical across the full
run. Source under `src` is unchanged. Actual Gitleaks is not run locally in this
continuation; the new published-head CI must verify its real scan and dependency
steps. Earlier green jobs do not certify this tree.

Fifteen new deliberate breakages were executed in isolated copies; all produced
assertion failures with zero collection errors. The working source was never
mutated. Exact machine-readable results are retained in
`docs/evidence/zerodha-security-suite.json` and
`docs/evidence/zerodha-security-sabotage.json`.

| Deliberate breakage | Regression that failed |
| --- | --- |
| findings_block_success | `tests/test_secret_scan_gate.py::test_any_finding_blocks_and_other_scan_still_runs` |
| both_scan_phases | `tests/test_secret_scan_gate.py::test_both_scans_are_required_for_a_clean_result` |
| status_report_agreement | `tests/test_secret_scan_gate.py::test_errors_or_inconsistent_reports_cannot_become_success` |
| metadata_only | `tests/test_secret_scan_gate.py::test_any_finding_blocks_and_other_scan_still_runs` |
| full_redaction_flag | `tests/test_secret_scan_gate.py::test_both_scans_are_required_for_a_clean_result` |
| reports_outside_scanned_tree | `tests/test_secret_scan_gate.py::test_reports_cannot_contaminate_the_scanned_tree` |
| summary_symlink_refusal | `tests/test_secret_scan_gate.py::test_summary_symlink_cannot_overwrite_an_unrelated_file` |
| mandatory_scanner_self_test | `tests/test_secret_scan_gate.py::test_cli_self_test_failure_cannot_reach_repository_scan` |
| same_path_sentinel_detection | `tests/test_secret_scan_gate.py::test_actual_scanner_self_test_cannot_accept_a_missing_same_path_detection` |
| cli_failure_exit | `tests/test_secret_scan_gate.py::test_cli_exit_status_tracks_both_scan_result` |
| bounded_metadata | `tests/test_secret_scan_gate.py::test_malformed_metadata_is_not_published_as_a_success` |
| valid_line_numbers | `tests/test_secret_scan_gate.py::test_malformed_metadata_is_not_published_as_a_success` |
| private_summary_permissions | `tests/test_secret_scan_gate.py::test_both_scans_are_required_for_a_clean_result` |
| exact_single_historical_exception | `tests/test_secret_scan_gate.py::test_only_one_exact_verified_fingerprint_is_exempted` |
| historical_evidence_preservation | `tests/test_secret_scan_gate.py::test_reformatted_evidence_reconstructs_the_entire_original_document` |

## Release boundary

The original token renewal, Compose-environment and dotenv fixes remain unchanged.
No merge, deployment, service restart, real login, live-money setting, instrument
admission or risk-limit change is performed. Real-host daily login/2FA, environment
publication, consumer recreation, alert receipt and pre-open operation remain owner
acceptance gates. Items 2–4 remain open; Item 3 must wait for the owner to merge Item 2.
A green configured CI is not a claim of full platform readiness or exhaustive security.
