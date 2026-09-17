# Item 2: additional required-risk regressions; mutation certification remains open

This continuation is based on `0e4fd70b44cd73a36518a557800a330611079350` in existing PR #133.
It adds only a focused test file and evidence. No production source, configuration,
threshold, watchlist, provider, ledger behavior or deployment file is modified.

## Verified behavior

The added tests exercise the actual required-risk configuration checks, history adapter
and persisted runtime reporting. They cover callable-history requirements, complete
sector coverage, positive freshness budgets, aware/nonfuture timestamps, clearing old
observations after a failed fetch, same-day/nonfuture fallback observations, duplicate
JSON keys in both environment input forms, grouping types, truthful unarmed telemetry,
refusal before analysis assembly, and refusal rather than optional approval after both
history and map disappear. All inputs are synthetic; no provider or broker request is made.

These tests are stronger normal regression checks, not a replacement for the required
independent source-guard-removal campaign. The initial 21-case version passed as part
of 94 focused checks. One further regression was added; the final focused run is 117
passing checks, including all 22 preflight acceptance cases and the existing risk tests.
An initial import-order lint issue was fixed; full Ruff subsequently passed.

## Exact source and full-suite results

Baseline: **1790 passed / 13 failed**, two warnings, 14 subtests passed.
Candidate: **1812 passed / the identical 13 failed**, two warnings, 14 subtests passed.
The exact JUnit failure identities compare equal. All failures are the existing Mac
deployment-portability cases. Nothing was skipped, xfailed or waived. The 22-case
increase comes exclusively from the added test file. All 466 selected source/test/
script/config hashes remained unchanged across verification.

The requested `/usr/local/bin/ruff` remains absent. Lint used the absolute existing
project virtual-environment interpreter, not an implicitly selected PATH executable.
The original production files, preflight acceptance assertions and CI configuration
remain unchanged. This test-only addition needs its own published-head CI before its
integration is considered verified.

## Required sabotage status: not completed

A mutation command timed out during intermittent Mac connectivity. A later explicit
read established that its script had not been created, no runner was active, and no
mutation report existed. A subsequent attempt to write the isolated-copy mutation
runner was blocked by the execution tool's safety checks. That blocked operation was
not retried or routed through another tool. No guard-removal success count is claimed.
No production guard was removed or disabled in the working source.

The normal regression results below must not be described as a completed sabotage
table. Earlier preflight and smoke/CI mutation records certify their own narrower
scopes, not the outstanding production portfolio-risk certification.

## Added executable cases — normal runs, not mutations

| Case in `tests/test_pilot_risk_guard_certification.py` | Normal result |
| --- | --- |
| `test_required_history_must_be_callable[None]` | passed |
| `test_required_history_must_be_callable[source1]` | passed |
| `test_required_mapping_coverage_is_checked_directly[all]` | passed |
| `test_required_mapping_coverage_is_checked_directly[TCS]` | passed |
| `test_freshness_budget_must_be_positive[delta0]` | passed |
| `test_freshness_budget_must_be_positive[delta1]` | passed |
| `test_history_rows_refuse_a_naive_clock_even_for_empty_input` | passed |
| `test_timestamp_guard_has_its_own_refusal_before_session_validation[naive]` | passed |
| `test_timestamp_guard_has_its_own_refusal_before_session_validation[future]` | passed |
| `test_failed_refetch_clears_previous_successful_observation` | passed |
| `test_observation_fallback_refuses_another_day_or_future_read[observed_at0]` | passed |
| `test_observation_fallback_refuses_another_day_or_future_read[observed_at1]` | passed |
| `test_duplicate_sector_keys_are_refused_in_both_environment_sources[inline]` | passed |
| `test_duplicate_sector_keys_are_refused_in_both_environment_sources[file]` | passed |
| `test_grouping_type_checks_have_specific_refusals[mapping0-sector_map_must_be_object]` | passed |
| `test_grouping_type_checks_have_specific_refusals[mapping1-sector_map_requires_string_pairs]` | passed |
| `test_grouping_type_checks_have_specific_refusals[mapping2-sector_map_requires_string_pairs]` | passed |
| `test_incomplete_sector_map_is_not_reported_armed_in_required_runtime` | passed |
| `test_noncallable_provider_is_not_reported_armed` | passed |
| `test_missing_required_inputs_refuse_before_analysis_pipeline[history]` | passed |
| `test_missing_required_inputs_refuse_before_analysis_pipeline[map]` | passed |
| `test_required_mode_never_returns_optional_approval_after_all_inputs_disappear` | passed |

## Open acceptance gates

Keep Item 2 draft and unmerged. The last published real-history attempt still returned
zero usable records after provider rate limiting; no new real-history attempt was
performed in this continuation. Production risk-guard-removal certification, actual
current-host configuration, real history, protective-exit acceptance, deployment/restore/
alert delivery and the inherited 13 Mac portability failures remain open.
Item 3 must wait for the owner to merge Item 2. No watchlist expansion, MCX admission,
fee/margin input, credential access, real order or live-money activation was performed.
