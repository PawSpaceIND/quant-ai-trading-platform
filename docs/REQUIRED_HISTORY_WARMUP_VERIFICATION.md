# PR #141: required-history warmup lifecycle verification

## Scope and authorship

This continues existing PR #141 on `fix/off-hours-book-risk-history-warmup`.
Initial reviewed head: `fdacd0a253fcc0c0ac36b22e467d57f37badd54e`.
Parallel work published the corrected runner at
`2e12e43a8e65e591794964ed4d2e1e4b37e4d61d` while this independent verification ran.
The tested `daemon.py` is byte-identical to that peer commit. All its regression
functions are retained unchanged. This addition provides extended tests and evidence,
not a second runner implementation or a duplicate PR. The peer worktree is untouched.

## Corrected behavior verified

Required DailyCloseHistory is warmed in the actual runner process at startup and
before cadence. The protection thread and stream supervisors are started before
awaiting history; telemetry is allowed to say unready while genuine data is absent.
There is no simulated success, transfer of a separate-process preflight cache, or
network call from telemetry. Required-source identity and existing daily caching are
preserved. Optional or unrecognized adapters are not implicitly enabled.

Provider/wiring exceptions yield fixed warning messages, not external tracebacks.
A missing instrument, abstention or failed fetch is still unready; one failed symbol
does not prevent checking the remainder. Stop requests prevent later symbol fetches,
post-startup cadence and post-warmup execution. The clock used for execution and proofs
is read again after warming. Both startup and cadence move blocking I/O off the event
loop, and cancellation propagates through cleanup of the running supervisors.

A synchronous request already in progress can complete after stop or cancellation;
this does not forcibly terminate a Python thread. The tested guarantee is no NEW
symbol request after stop and no subsequent cadence dispatch, plus normal cleanup.
The existing adapter's request timeouts, rate limit and refusal behavior are unchanged.
These tests do not establish an operating-system sandbox or every possible scheduling
interleaving. Python coroutine cancellation/thread behavior was checked against the
primary documentation: https://docs.python.org/3.12/library/asyncio-task.html

## Executed evidence

The original seven failing lifecycle checks from the previous independent review
are preserved and now pass. The initial parent baseline was read from completed
Linux CI job 105301736525 / run 35250609545: **2941 passed**, one warning,
14 additional passing subtests, 631.54 seconds. That baseline was not rerun locally.

Full final Mac run: **2982 ordinary + 22 pilot-preflight + 17 institutional acceptance
= 3021 passed**, zero failures/errors/skips; two dependency-deprecation warnings and
14 additional passing subtests, 532.36 seconds. All **51 existing portfolio-risk
mutations**, **57 existing history-adapter mutations**, and **seven paper-exit cases**
passed in this complete run. New lifecycle behavior coverage totals **19 cases**,
including the previous seven. The new warmup mutation suite contains **22 cases**.

Relative to the initial head the increase is 41 cases; relative to the corrected peer
parent, which already contains seven of them, this verification adds **34 cases**.
No original regression function or assertion was removed, loosened, skipped or xfailed.
Ruff `src tests` and whitespace checks pass. `/usr/local/bin/ruff` is absent on the Mac;
the absolute existing project-venv interpreter is used instead and its result is recorded.

521 source/test/configuration hashes were captured before the full run and checked
again at completion. Advancing the detached review metadata to the identical peer
source changed none of those files. Complete hashes/counts and paired mutation results
are in `docs/evidence/required-history-warmup-certification.json`.

Only completed scratch from this lane's earlier 2977-test run was archived to relieve
disk pressure. All 38,418 regular files, including hard-linked archive members, were
checksum-verified before removing that disposable directory. Its source, JUnit and log
files were not removed. An initial archive verifier was stopped for repeated-decompression
cost; the completed archive was then verified sequentially, including its hard links.
An interim combined status command was tool-blocked and not retried; no permission was
changed. Neither issue is counted as a test pass or a production failure.

## Guard-removal verification: 22 / 22

Every case first passes its specified test on copied unchanged source; the designated
mutation then fails an assertion with exit 1 and no collection errors or skipped tests.
The copied source is restored and original hashes remain unchanged. The runner reuses
the existing portfolio-risk mutation harness, in a credential-free child environment
with Python socket connect/DNS/send events denied. These are offline tests, not AWS
requests or real orders. All 22 passed separately and again within the full run.

| Deliberately broken guard | Specific failing regression |
| --- | --- |
| `required_only` | `tests/test_required_history_warmup_lifecycle.py::test_warmup_does_not_enable_optional_history` |
| `known_adapter_only` | `tests/test_required_history_warmup_lifecycle.py::test_warmup_ignores_unrecognized_history_adapter` |
| `callable_fetch` | `tests/test_required_history_warmup_lifecycle.py::test_noncallable_fetch_reports_specific_refusal` |
| `required_provider_identity` | `tests/test_required_history_warmup_lifecycle.py::test_required_source_is_not_replaced_by_regime_source` |
| `missing_instrument` | `tests/test_required_history_warmup_lifecycle.py::test_missing_identity_is_not_fetched_or_marked_ready` |
| `symbol_stop_guard` | `tests/test_required_history_warmup_lifecycle.py::test_stop_during_warmup_prevents_later_symbol_fetches` |
| `provider_exception_redaction` | `tests/test_required_history_warmup_lifecycle.py::test_warmup_never_logs_external_exception_details[provider]` |
| `wiring_exception_redaction` | `tests/test_required_history_warmup_lifecycle.py::test_warmup_never_logs_external_exception_details[wiring]` |
| `remaining_scope_after_failure` | `tests/test_required_history_warmup_lifecycle.py::test_one_failed_symbol_does_not_hide_the_remaining_scope` |
| `abstention_diagnostic` | `tests/test_required_history_warmup_lifecycle.py::test_abstention_is_not_invented_readiness` |
| `existing_daily_cache` | `tests/test_pilot_required_risk_gates.py::test_required_history_warmup_reuses_daily_cache_and_refreshes_next_day` |
| `startup_stop_before_warmup` | `tests/test_required_history_warmup_lifecycle.py::test_already_stopped_runner_skips_initial_warmup` |
| `startup_warmup_wiring` | `tests/test_pilot_required_risk_gates.py::test_off_hours_runner_start_warms_required_history_before_telemetry` |
| `startup_off_event_loop` | `tests/test_required_history_warmup_lifecycle.py::test_history_wait_keeps_event_loop_and_broker_lock_available[True]` |
| `cadence_off_event_loop` | `tests/test_required_history_warmup_lifecycle.py::test_history_wait_keeps_event_loop_and_broker_lock_available[False]` |
| `cadence_warmup_wiring` | `tests/test_pilot_required_risk_gates.py::test_aligned_cadence_refreshes_required_history_before_run` |
| `startup_stop_after_warmup` | `tests/test_required_history_warmup_lifecycle.py::test_stopped_startup_does_not_launch_cadence` |
| `cadence_stop_after_warmup` | `tests/test_required_history_warmup_lifecycle.py::test_cadence_uses_post_warmup_clock_and_honors_stop[True]` |
| `post_warmup_execution_clock` | `tests/test_required_history_warmup_lifecycle.py::test_cadence_uses_post_warmup_clock_and_honors_stop[False]` |
| `protection_before_initial_io` | `tests/test_required_history_warmup_lifecycle.py::test_protection_starts_while_initial_history_request_is_stalled` |
| `streams_before_initial_io` | `tests/test_required_history_warmup_lifecycle.py::test_stream_supervisor_runs_before_initial_history_completes` |
| `startup_warmup_inside_cleanup` | `tests/test_required_history_warmup_lifecycle.py::test_cancelled_startup_cleans_up_and_stops_later_requests` |

## Remaining acceptance / owner boundary

No AWS service, ledger, production credential, risk cap, watchlist, money mode or
provider setting was changed here. The last owner-observed deployment remains a
separate state from this code verification; no new deployment is claimed.

Review and owner merge/deployment are separate actions. After rollout, read the live
daemon's fresh runtime report: all three required gates must be armed, and the two
history-backed gates must show genuine usable records and all five names covered.
An earlier isolated 600-record AWS preflight does not establish that in-process cache.
Alert delivery, renewable-session/pre-open operation and host recovery still need their
own acceptance. Keep the five-name pilot and paper mode; do not widen scope to disguise
a readiness failure. Require the new published commit's completed CI, not its parent's
success. CI status is recorded in the PR rather than predicted here.
