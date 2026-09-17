# Item 2: offline container smoke repair and visible preflight acceptance

## Scope

This continuation stays on PR #133 / `feat/arm-risk-gates`. It reconciles the
existing branch with owner main `13bbdec81fe59332513260b746d51ec75bcf565d` through
local integration commit `0c6c130e5e7e4c37c16d4bd7ff69274c4d98d195`. The main-side
research/specialist changes had no filename overlap with the Item 2 changes.
This is a branch refresh, NOT a PR merge into main. No deployment or live action.

The interrupted smoke patch was preserved and reviewed in a dedicated checkout.
Its peer worktree was not overwritten. This patch changes tests and CI reporting,
not `src`, the preflight implementation, risk thresholds or operator configuration.

## Container failure and repair

The published-parent run 35190196486 failed in container job 105100852092 at
`container_dashboard_smoke.mjs:122`: the fixture reported an armed
`sector_concentration` gate while the old smoke assertion expected no armed gates.
The fixture really loads the committed sector map, but intentionally supplies no
history provider and never opens market-data streams or performs AI analysis.

The actual dashboard smoke now calls a shared Node assertion. It checks that the
sector gate is the sole armed gate, with five mapping records, three groups and
the unchanged 0.25 limit. Both history gate IDs must still be present and unarmed.
Schema, gate inventory, boolean arming and setting identifiers remain checked.
Thirteen executable Python cases invoke that real Node assertion with valid or
corrupted synthetic payloads. This does not make either live history gate ready.

## Unresolved acceptance is now a real CI failure

The unchanged 22-case `acceptance/test_risk_preflight_input_boundary.py` is now
explicitly selected by a separate `pilot-preflight-acceptance` workflow job.
Its failures are not xfailed, skipped, masked or removed; the JUnit report uploads
even when the job fails, and a missing report is an error. Three workflow/smoke
wiring regressions check that this invocation cannot disappear unnoticed.
No branch-protection administration was performed; this adds a failing workflow
job, not a claim of a newly configured required branch-protection rule.

The preflight suite still reports **20 failed / 2 passed**, zero collection errors.
These are open synthetic input/report contracts, not 20 separate production
incidents or a live-risk bypass. The implementation repair remains outstanding;
the previously blocked production edit was not retried through another route.

## Exact local evidence

- Pre-sync published-parent baseline: **13 failed, 1584 passed, 2 warnings, 14 subtests passed in 53.38s**.
- Retained refreshed-base baseline: **13 failed, 1754 passed, 2 warnings, 14 subtests passed in 194.99s (0:03:14)**.
- Current full candidate: **13 failed, 1770 passed, 2 warnings, 14 subtests passed in 179.30s (0:02:59)**.

The refresh brings 170 ordinary passing cases from owner main. This patch adds
16 ordinary cases, not 186. All three ordinary runs have the same 13 named Mac
deployment-portability failures; their full failure identities are retained.
The focused set passes **64/64**. Ruff `src tests` and whitespace checks pass.
Ruff used the absolute existing project virtual-environment interpreter because
`/usr/local/bin/ruff` is absent; no shadowed executable or global installation.
All **465** selected source/test/script/config files remain hash-identical across
the full candidate run. All six pre-existing CI jobs are unchanged; one explicit
acceptance job is added. The new acceptance assertions remain byte-identical to
the prior published version.

## Deliberate breakage verification

The expanded campaign executed **19/19** detected mutations, all with assertion
failures and zero collection errors. Each ran in a disposable source copy; working
sources stayed unchanged. This certifies smoke assertions and CI selection ONLY.
It is not the still-required production risk-guard-removal campaign for Item 2.

| Removed/broken check | Specific regression that failed |
| --- | --- |
| schema | `tests/test_container_risk_gate_assertions.py::test_offline_dashboard_fixture_assertion_is_exact[wrong_schema]` |
| gate inventory | `tests/test_container_risk_gate_assertions.py::test_offline_dashboard_fixture_assertion_is_exact[too_few]` |
| boolean arming | `tests/test_container_risk_gate_assertions.py::test_offline_dashboard_fixture_assertion_is_exact[non_boolean]` |
| setting identity | `tests/test_container_risk_gate_assertions.py::test_offline_dashboard_fixture_assertion_is_exact[invalid_setting]` |
| exact armed set | `tests/test_container_risk_gate_assertions.py::test_offline_dashboard_fixture_assertion_is_exact[unarmed_sector]` |
| sector records | `tests/test_container_risk_gate_assertions.py::test_offline_dashboard_fixture_assertion_is_exact[wrong_records]` |
| sector groups | `tests/test_container_risk_gate_assertions.py::test_offline_dashboard_fixture_assertion_is_exact[wrong_groups]` |
| unchanged limit | `tests/test_container_risk_gate_assertions.py::test_offline_dashboard_fixture_assertion_is_exact[raised_limit]` |
| history inventory | `tests/test_container_risk_gate_assertions.py::test_offline_dashboard_fixture_assertion_is_exact[missing_history]` |
| smoke call wiring | `tests/test_pilot_acceptance_workflow.py::test_container_dashboard_calls_the_shared_risk_assertions` |
| acceptance job selected | `tests/test_pilot_acceptance_workflow.py::test_preflight_acceptance_is_explicit_and_failure_is_not_masked` |
| failing exit preserved | `tests/test_pilot_acceptance_workflow.py::test_preflight_acceptance_is_explicit_and_failure_is_not_masked` |
| Report artifact identity | `tests/test_pilot_acceptance_workflow.py::test_preflight_acceptance_retains_report_even_on_failure` |
| Artifact uploaded even after test failure | `tests/test_pilot_acceptance_workflow.py::test_preflight_acceptance_retains_report_even_on_failure` |
| Missing report cannot be silently accepted | `tests/test_pilot_acceptance_workflow.py::test_preflight_acceptance_retains_report_even_on_failure` |
| acceptance job not skippable | `tests/test_pilot_acceptance_workflow.py::test_preflight_acceptance_is_explicit_and_failure_is_not_masked` |
| acceptance step not skippable | `tests/test_pilot_acceptance_workflow.py::test_preflight_acceptance_is_explicit_and_failure_is_not_masked` |
| acceptance job failure not ignored | `tests/test_pilot_acceptance_workflow.py::test_preflight_acceptance_is_explicit_and_failure_is_not_masked` |
| acceptance step failure not ignored | `tests/test_pilot_acceptance_workflow.py::test_preflight_acceptance_is_explicit_and_failure_is_not_masked` |

Machine-readable results are in `docs/evidence/pilot-container-smoke-checkpoint.json`
and `docs/evidence/pilot-container-smoke-sabotage.json`. JUnit can include the
14 subtests; ordinary pytest totals above are taken from the printed summaries.

## Remaining acceptance and release gates

Actual Docker verification is delegated to the new published-head CI; the local
checks do not certify container execution. Keep the PR draft even if the container
job passes, because preflight acceptance remains red. Complete that repair, the
production risk-guard-removal campaign and real usable history before claiming
Item 2 closure. The prior real-history attempts returned zero usable records;
this continuation makes no new provider request and manufactures no data.

Target-host deployment, private configuration compatibility, real records and
covered exits must still be accepted separately. No actual broker request, paid
model call, credential read, production ledger change, service restart, fee/margin
invention, watchlist expansion or MCX admission is performed. Item 3 remains blocked
until the owner merges Item 2. The inherited Mac portability failures remain open.
