# Secret-scan diagnosis and visible institutional acceptance

Continuation of draft PR #126 from d7f2b9803ced0090721da88e1765872f2b7898ad.
Only scanner diagnostics/configuration, regression tests, CI acceptance selection and
this documentation change. No trading risk implementation, daemon or live route changes.

## Verified historical false positive

Gitleaks 8.30.1 reported one generic-api-key finding at line 264 of
`docs/evidence/zerodha-token-renewal-sabotage.json` in commit
`916ff1681cacf1395476dfdbf41378eddd5a6bb0`. That commit belongs to the token-renewal
branch and is not an ancestor of the current integration head or main.

The matched value is the `restored_sha256` entry for `scripts/renew_pilot_token.py`.
It is a 64-character hexadecimal file checksum. Independently hashing that script's
bytes from the same Git commit produces exactly the recorded value:
`88fa3709f206fb7f9356f749a38231557aff667bcf0a8d65080166f903671427`.
No account credential was accessed, used, rotated or printed to establish this.

`.gitleaksignore` exempts only that exact commit/path/rule/line fingerprint. No file,
branch, generic-api-key rule, digest pattern or commit range is broadly excluded.
Remove the exception if that finding is no longer reachable in the scanned history;
reinvestigate rather than extend it if the file, commit, rule or line changes.

## Scanner behavior and diagnostics

The downloaded scanner version and checksum verification remain unchanged. The
wrapper runs the existing full Git history scan and the working-tree scan before
dependency installation. Both run even when one fails. Findings, execution errors,
timeouts, missing/malformed reports or inconsistent exit codes all block success.
No baseline, reduced-history range, blanket allowlist or success override is added.

Before repository scanning, the actual scanner must detect a generated noncredential
sentinel in another commit of the same evidence path, in both Git and directory mode.
The sentinel is derived from a public test phrase inside a disposable repository;
it is not a real key. This verifies that the historical exception is not a file waiver.

Reports are redacted and temporary. Only a bounded metadata summary containing file,
line, rule, commit and fingerprint is published. Matched text, secret values,
scanner stdout/stderr and free-form descriptions are not copied to CI output or the
uploaded summary. Raw redacted reports are deleted after metadata extraction.

## Risk acceptance now runs in CI

`institutional-risk-acceptance` explicitly runs the existing 17-case institutional
acceptance file using paper-only synthetic fixtures. Its assertions are unchanged.
It still has 15 failing cases across four known requirements and two passing cases.
A failure is a real failing CI job, with a JUnit artifact, not an expected-failure
waiver. This change does not repair or retry the previously tool-blocked risk code.
No repository branch-protection administration was performed; workflow failure is
not represented as a newly configured required branch-protection rule.

## Evidence and retained boundaries

The scanner helper has behavioral tests for redaction, both scan phases, nonzero
exits, invalid reports, timeouts, and contradictory status/report combinations.
The actual Gitleaks binary is additionally exercised by the sentinel and full scans.
Exact source hashes, normal-suite results and exact-head CI results are recorded in
the PR checkpoint. Passing security does not clear failing risk acceptance.

Full institutional-daemon integration, modeled-loss/factor/Kelly/exposure repairs,
Mac deployment portability, migration/rollback, market/settlement coverage and real
data, operational, security and forward-trading acceptance remain separate gates.
No deployment, broker order, model promotion or live-money enablement is performed.
