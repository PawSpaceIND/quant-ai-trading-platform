# Item 2 preflight: strict inputs and complete report publication

## Scope

Continuation of existing PR #133 (`feat/arm-risk-gates`) at
`00fa3b34cbcf2a36bb365f5cb0bbca02ba687166`. This repairs only the preflight CLI's
input/report boundary. Risk calculations, thresholds, source providers, sector map,
watchlist, container fixtures, CI jobs and original acceptance assertions are unchanged.
No owner-main merge, deployment, broker request, real market-data request, production
credential access or live-money change was performed.

## Reproduced failure and actual repair

The unchanged 22-case acceptance suite reports 20 failed / 2 passed on the base and
22 passed on the repaired implementation. No skip, xfail or success wrapper was added.

Both JSON inputs are bounded to 1 MiB, read as regular files, and parsed as objects
with duplicate-key rejection at every object level. Non-JSON numeric constants refuse;
fractional numbers are decoded as Decimal instead of being rounded through binary
floating point. The 1 MiB bound is the previously proposed operator-input resource
budget, not an exchange rule or financial threshold.

Reports contain SHA-256 digests of the exact directives and sector-map bytes used,
not copies of those inputs. Both files are re-read after readiness checking, including
stdout-only mode. When publishing a file, they are also rechecked after report flushing
and immediately before publication. A changed, missing or invalid input refuses without
printing a successful report.

The final destination must be new. A private same-directory staging file is created
before constructing the history provider, so an existing/unwritable/missing output
location fails before history work. Complete bytes are written, flushed, synced and
read back before a no-replace hard link publishes the final name. Another writer's
file is never overwritten. The containing directory is synced; temporary files are
removed on ordinary success and exception paths. No existing output is deleted.

## Important publication and interpretation boundaries

- Exit 0 means this invocation returned dataReady=true and completed requested report
  publication; it is not approval to merge, deploy, place an order or widen scope.
- Exit 1 preserves a complete diagnostic report with dataReady=false. Missing history
  remains missing. No bar, fee, margin figure or readiness result is manufactured.
- Exit 2 covers invalid inputs or publication problems and does not print the success
  JSON. A failure before the hard link leaves no final report. A directory-sync failure
  AFTER the link can leave a complete private report, but still returns nonzero because
  power-loss durability was not confirmed. Do not treat the file's existence as success.
- unchangedAtCompletion reports byte equality at the explicit checks. It is not a lock
  against unrelated edits, proof that an input never changed temporarily, or a global
  transaction across two independent source files. The hashes identify the snapshot.
- Publication targets a trusted operator-owned local directory on the supported POSIX
  hosts. Filesystems without hard-link or directory-fsync support refuse; no unsafe
  overwrite fallback is provided. Crash/power-loss drills on the target host remain open.
- Real source qualification and running-host acceptance remain separate. These tests
  substitute the history checker and make no public-provider or broker request.

## Executed local evidence

Baseline: **13 failed, 1770 passed, 2 warnings, 14 subtests passed in 203.39s (0:03:23)**.

Candidate: **13 failed, 1790 passed, 2 warnings, 14 subtests passed in 176.94s (0:02:56)**.

The same 13 Mac deployment-portability failure identities were compared exactly;
no ordinary tests were skipped or waived. This patch adds 20 passing ordinary cases.
The combined preflight/required-risk/CI selection set is 84 passed. Ruff src/tests and
the preflight script, plus whitespace checks, pass. The requested /usr/local/bin/ruff
is absent; the absolute existing project virtual-environment interpreter was used.

All 465 selected source/test/script/config hashes were unchanged across full verification.
The original acceptance suite is byte-identical. Exact results and mutation metadata
are in `docs/evidence/pilot-preflight-publication-suite.json` and
`docs/evidence/pilot-preflight-publication-sabotage.json`.

All 24 listed deliberate breakages were caught by specific assertion failures in
isolated copies, with zero collection errors. The first campaign caught 23/24:
the oversized CLI fixture also failed JSON parsing after truncation, masking removal
of its size guard. The already-added direct byte-budget test independently detected
that removal on rerun. The initial result is preserved; no assertion was weakened.
These certify preflight CLI guards, NOT the outstanding portfolio-risk guard campaign.

A later tool safety check blocked adding two further verification tests. That edit
was not applied or retried. Completed test counts do not include those proposed cases;
no complete platform/operational certification follows from this patch.

| Deliberately broken guard | Specific regression that failed |
| --- | --- |
| regular_input | `tests/test_pilot_preflight_publication.py::test_special_input_is_refused_without_reading_a_fifo` |
| bounded_read | `tests/test_pilot_preflight_publication.py::test_input_reader_accepts_exactly_the_budget_and_rejects_one_more` |
| input_size_limit | `tests/test_pilot_preflight_publication.py::test_input_reader_accepts_exactly_the_budget_and_rejects_one_more` |
| duplicate_keys | `acceptance/test_risk_preflight_input_boundary.py::test_duplicate_directive_keys_refuse_before_history` |
| non_json_numbers | `acceptance/test_risk_preflight_input_boundary.py::test_non_json_numbers_refuse_before_history` |
| object_shape | `tests/test_pilot_preflight_publication.py::test_object_validation_has_a_consistent_contract` |
| decimal_precision | `tests/test_pilot_preflight_publication.py::test_directive_decimal_is_not_rounded_through_binary_float` |
| output_precheck | `acceptance/test_risk_preflight_input_boundary.py::test_unusable_output_refuses_before_history` |
| staging_before_provider | `tests/test_pilot_preflight_publication.py::test_staging_failure_happens_before_provider_construction` |
| private_report_mode | `acceptance/test_risk_preflight_input_boundary.py::test_published_report_has_private_permissions` |
| input_recheck_for_stdout | `tests/test_pilot_preflight_publication.py::test_stdout_only_report_also_rechecks_both_input_files` |
| input_recheck_before_link | `tests/test_pilot_preflight_publication.py::test_inputs_changed_during_report_flush_refuse_publication` |
| directives_hash | `acceptance/test_risk_preflight_input_boundary.py::test_reports_bind_exact_input_bytes_without_copying_them` |
| sector_hash | `acceptance/test_risk_preflight_input_boundary.py::test_reports_bind_exact_input_bytes_without_copying_them` |
| short_write_refusal | `tests/test_pilot_preflight_publication.py::test_incomplete_report_cannot_reach_final_filename[short]` |
| file_fsync | `acceptance/test_risk_preflight_input_boundary.py::test_write_failure_never_publishes_a_partial_report` |
| report_readback | `tests/test_pilot_preflight_publication.py::test_incomplete_report_cannot_reach_final_filename[incomplete_but_claimed_complete]` |
| no_replace_publication | `acceptance/test_risk_preflight_input_boundary.py::test_a_concurrent_report_is_not_replaced` |
| directory_fsync | `tests/test_pilot_preflight_publication.py::test_directory_flush_failure_is_nonzero_with_only_a_complete_report` |
| cleanup_on_failure | `acceptance/test_risk_preflight_input_boundary.py::test_write_failure_never_publishes_a_partial_report` |
| strict_report_json | `tests/test_pilot_preflight_publication.py::test_nonfinite_report_cannot_be_printed_or_published` |
| failure_before_stdout | `acceptance/test_risk_preflight_input_boundary.py::test_write_failure_never_publishes_a_partial_report` |
| unready_nonzero | `acceptance/test_risk_preflight_input_boundary.py::test_unready_result_remains_nonzero_with_bound_inputs` |
| online_consent | `acceptance/test_risk_preflight_input_boundary.py::test_no_online_authority_means_no_provider_or_output` |

## Sources and remaining gates

Python JSON behavior and decoder hooks:
https://docs.python.org/3/library/json.html
Temporary-file ownership/creation:
https://docs.python.org/3/library/tempfile.html#tempfile.mkstemp
File synchronization and link APIs:
https://docs.python.org/3/library/os.html

The new published head must pass all seven existing CI jobs, including preflight
acceptance. Prior green jobs do not certify it. Keep PR #133 draft: usable real
historical records, production portfolio-risk guard-removal verification, target-host
acceptance, and the inherited Mac deployment-portability cases remain open.
Item 3 must wait until the owner merges Item 2. No deployment is performed here.
