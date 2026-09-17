# Zerodha renewal integration with the institutional main checkpoint

This refresh stays on PR #129 and `feat/zerodha-token-renewal`.
It combines renewal head `76119b613ac8a297de763a7828796a851569c52e` with
main `a20a0d833b83a094c372c54058c38bdc3c1d1afb` (merged #126/#135 and #134).
It does not merge the PR into main, deploy Lightsail, widen pilot scope, enable
institutional execution, approve a model or place an order.

## Conflict resolution

The actual merge conflicts are the environment factory in `daemon.py` and the
add/add `test_secret_scan_gate.py`. No entire-side overwrite is used for startup.

Startup keeps main's order-identity/storage validation before notification or
provider construction. The existing renewal profile check follows that local
validation, still before broker/stream assembly. Its validated credentials feed
the same existing builder; main's bound-mode and OMS arguments are retained.
The selected identity mode remains operator-controlled; no migration is implied.

The combined security test file is a function-by-function union: every original
function AST from both parents is retained, including main's institutional-risk
CI-gate assertion and renewal's additional scanner/sentinel/privacy tests.
The actual CI definition and scanner helper match main byte-for-byte. Compose
retains main's order-identity settings and adds only the existing renewal token
metadata and independent watcher relative to that baseline.

## Executable integration coverage

The first combined focused run had 230 passes and two failures. Both were the
new main environment-factory tests, which previously had no token profile check
and therefore no synthetic profile endpoint. Their fixture now reuses renewal's
existing `FakeKite` while executing the real validator. No assertion changed.
The corrected focused run passes 242 cases, including the original institutional
risk acceptance file and ten new startup-interaction cases.

Four new cases reject invalid mode, missing OMS, aliased storage or a legacy-mode
OMS before notifications/profile/provider construction. Six cases check missing,
rejected and wrong-account tokens in both legacy and bound modes. They require
one CRITICAL boot alert, no exposed synthetic token and no ledger/OMS creation.

Two separate disposable-interpreter experiments remove one startup check at a
time. Removing identity validation fails all four designated cases; replacing
token validation with an unconditional synthetic approval fails all six token
cases. Passing controls precede these experiments; neither collection errors
nor skipped tests count as detection. Repository source is not mutated by them.
These experiments do not execute the separately outstanding portfolio-risk
mutation campaign and do not replace it.

The full combined local suite passes 2,551 ordinary tests and 14 subtests with
zero failures/errors/skips and two existing dependency deprecation warnings.
Ruff passes using the existing absolute project interpreter; the requested
`/usr/local/bin/ruff` is absent. Whitespace checks pass. The helper's `--help`
also runs with site-packages disabled; that is import/CLI evidence, not login.
Local results: `docs/evidence/zerodha-post126-integration.json`.
The refreshed head's CI, baseline comparison and review status are recorded in
the PR conversation; no older green run certifies a new integration.

## Host transition is separate

The owner has verified Kite 5.2.2 in
`/home/ubuntu/.venvs/pramana-login/bin/python`; this refresh does not reinstall it.
No credentials are read and no login is attempted in these tests. The existing
`docs/ZERODHA_DAILY_RENEWAL.md` remains the operation runbook after reviewed merge
and deployment. A host-side interactive terminal is required for the hidden login
prompt; do not use a noninteractive `exec -T` for that human step.

Actual token validity, account continuity, atomic host publication, refreshed
consumers, watcher liveness and independent human alert receipt remain acceptance
gates. A healthy existing container or a successful SDK import closes none of
those on its own. Keep live money disabled and preserve all host data.
