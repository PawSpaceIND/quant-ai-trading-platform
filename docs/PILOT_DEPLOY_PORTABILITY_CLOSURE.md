# Pilot deployment-script compatibility: Mac failures repaired

## Scope

Existing Item 2 PR #133, based on `7ac330fc59cf85f7aeaa593d4df10318e217dcee`.
This patch repairs the inherited local deployment-verifier failures without running
an actual deployment. It changes the shell script, adds one test file and retains
this document plus two evidence records. It does not edit portfolio-risk source,
thresholds, the map, watchlist, instrument identity, provider wiring, preflight
assertions, CI definitions or any real environment file. No duplicate PR is opened.

## Reproduction and repair

The fresh original deployment suite failed 13 cases and passed six under the Mac's
actual `/bin/bash` 3.2 and `/usr/bin/sed`. The first shared failure was `declare -A`,
which that shell rejects. Replacing associative maps with matching indexed arrays
for services, container IDs and initial restart counts cleared 12 cases. It preserved
the identity of each service through both inspections and all existing comparisons.

The remaining repeat-run case then exposed GNU-style `sed -i` arguments on BSD sed.
The script now supplies the separate empty backup suffix on Darwin and retains the
GNU invocation elsewhere. Neither branch deliberately creates a credential-bearing
backup. Tests use the native binaries rather than a newer shell that hides the defect.
All 19 original deployment assertions are unchanged and now pass.

Eleven additional cases exercise two revision updates with exact unrelated-byte,
mode and owner preservation; no backup files; unequal per-service restart baselines;
an increment in each service; first/last-service health; a missing middle container;
an empty service inventory; and the non-main refusal. The existing fixture supplies
throwaway local Git repositories and a fake Docker executable. No test contacts the
running pilot, actual Docker daemon, broker or data provider.

## Exact local verification

Baseline: **13 failed, 1829 passed, 2 warnings, 14 subtests passed in 204.02s (0:03:24)**.

Definitive candidate: **1853 passed, 2 warnings, 14 subtests passed in 254.55s (0:04:14)**.

All existing case identities are retained, with 11 added ordinary cases. The 13 old
failures are now passing: there is no baseline-failure waiver. Zero errors or skips.
The deployment-only combined suite passes 30 cases. Separately reported subtests are
not added to the ordinary test count. A fixture-import lint error was fixed without
changing assertions, then the complete final source was rerun.

Ruff src/tests, `/bin/bash -n` and whitespace checks pass. `/usr/local/bin/ruff` is
absent on this Mac, so lint uses the absolute existing project virtual-environment
interpreter. All 468 selected source/test/script/config hashes stayed unchanged during
final verification. The original test file and risk/runtime source remain unchanged.

## Deliberate compatibility regressions

Five deliberately reintroduced defects ran in disposable copies against the fake-host
fixtures. Every designated test failed with an assertion, not a collection error.
The working deployment script was never mutated. These are compatibility checks,
NOT execution or certification of the separately blocked 51-case portfolio-risk inventory.

| Deliberately reintroduced defect | Test that caught it |
| --- | --- |
| bash32_container_array | `tests/test_deploy_pilot_portability.py::test_native_tools_can_stamp_twice_without_backup_or_unrelated_byte_changes` |
| bash32_restart_array | `tests/test_deploy_pilot_portability.py::test_native_tools_can_stamp_twice_without_backup_or_unrelated_byte_changes` |
| bsd_sed_empty_suffix | `tests/test_deploy_pilot_portability.py::test_native_tools_can_stamp_twice_without_backup_or_unrelated_byte_changes` |
| container_identity_per_index | `tests/test_deploy_pilot_portability.py::test_each_service_retains_its_own_unchanged_restart_baseline` |
| restart_baseline_per_index | `tests/test_deploy_pilot_portability.py::test_each_service_retains_its_own_unchanged_restart_baseline` |

## Boundaries retained

Root-directory, clean-tree, main-branch and market-calendar checks are unchanged.
The existing owner `--force` option and its warning are unchanged. Quiet Compose
validation, non-running/mid-restart/unhealthy checks, restart-counter comparisons
and the reported release revision remain active. Indexed array service names stay
data rather than arithmetic expressions.

This is a compatibility repair, not a new transactional environment writer. The
existing revision-stamp behavior is preserved; power-loss and concurrent-writer
atomicity for deployment stamping are not newly certified here. The separate token
renewal PR retains ownership of its atomic credential updater. Do not run either
operation concurrently. No system installation or permanent PATH change is required
or performed by these tests. Native GNU sed behavior must also pass exact-head Linux CI.

Reference for longstanding indexed-array syntax:
https://ftp.gnu.org/pub/old-gnu/Manuals/bash-2.05a/html_node/bashref_71.html
Current distinction between indexed and associative declarations:
https://www.gnu.org/s/bash/manual/html_node/Arrays.html
The actual Mac sed invocation is verified by the native-tool reproduction and tests.

## Remaining four-item closure

Keep PR #133 draft and unmerged. Usable real history, the required portfolio-risk
source-guard-removal verification and actual host/covered-exit acceptance remain open.
No previously blocked risk-mutation operation was retried in this continuation.
The older documents' statements that the 13 Mac compatibility failures remain open
are superseded by this result for this candidate only; main is not changed here.

Exact-head CI, owner review and integration are separate from local verification.
Do not deploy, change live-money state, widen the five-name watchlist, invent provider
records, add MCX admission or modify thresholds. Item 3 remains gated on the owner's
merge of Item 2. The owner alone merges and deploys, as the original build prompt states.
