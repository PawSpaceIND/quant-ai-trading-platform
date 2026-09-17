# Deployment evidence: refuse false-success reports

Parent: `12653875b0530eb2cbc13c3dddced8fb70bb5844`, existing PR #133.
This repairs the deployment verifier, not the blocked historical-data adapter or portfolio-risk campaign.

## Reproduction and repair

The initial 21 synthetic cases produced 20 failures and one pass. A failed Docker
inspection with plausible stdout could still lead to an all-services-running report.
Malformed restart counts could fail shell arithmetic without failing its conditional.
Service enumeration lost the command status through process substitution, and the
first-line-only container lookup could hide additional containers.

The verifier now requires successful command exits before interpreting output.
Both inspection passes validate the exact state, boolean, count and health fields.
Counts must fit signed 64-bit shell integers; the maximum valid value and the next
invalid value are tested. This is a parser limit, not a financial threshold.
A decreasing count refuses because continuity is no longer established.
Malformed inspection text is not echoed in normal reports.

Service names must be valid and unique. Each service must resolve to exactly one
unambiguous container. Scaled/ambiguous services refuse instead of certifying only
the first container. Empty/missing services, unhealthy or non-running containers,
mid-restart states and rising restart counts remain refusals.

## Important scope boundaries

Verification still occurs AFTER the existing build/start step: a verification failure
on a real operator invocation does not mean no deployment side effects occurred.
Tests here use disposable Git repositories and fake Docker; no real deployment was
performed. Root, clean-tree, main-branch and calendar gates are unchanged. Stamping
transactionality, rollback, global race-free snapshots and actual-host health are
not newly certified. Risk code, limits, mappings and provider selection are unchanged.

Stricter identity checks exposed a test-stub issue on Bash 3.2: `${*##* }` expanded
more than the last argument. Both synthetic Docker expressions now use `${!#}`.
AST comparison confirms existing test/fixture functions and assertions are unchanged.
The correction was made to the fixture, not by relaxing production validation.

## Executed results

Prior exact-parent full Mac evidence: **1853 passed**, zero failures; not rerun here.
Final candidate: **1878 passed**, zero failures/errors/skips, two warnings, 14 subtests.
The ordinary suite adds **25 cases**. JUnit's suite count is 1892 including subtests;
it must not be presented as 1892 ordinary tests. The unchanged separate preflight
acceptance suite passes **22/22**. All **469** selected file hashes are unchanged
throughout full certification. Ruff, native Bash syntax and whitespace checks pass.
Requested `/usr/local/bin/ruff` is absent; the absolute existing venv interpreter
was used, without system-tool installation or persistent PATH changes.

Ten deliberate deployment-verifier breakages ran in disposable copies. Each caused
assertion failures and zero collection errors; the working source stayed intact.
Guard-specific diagnostics/ordering also ensure later refusals do not mask removed
validation. This is not the previously blocked production risk-guard campaign.

| Deliberately broken protection | Regression that failed |
| --- | --- |
| inspect_exit_status | `tests/test_deploy_evidence_integrity.py::test_failed_inspection_cannot_pass_even_with_valid_stdout[before]` |
| inspection_field_validation | `tests/test_deploy_evidence_integrity.py::test_malformed_inspection_refuses_without_echoing_payload[running false not-a-number healthy-before]` |
| restart_integer_range | `tests/test_deploy_evidence_integrity.py::test_malformed_inspection_refuses_without_echoing_payload[running false 9223372036854775808 healthy-after]` |
| service_inventory_exit | `tests/test_deploy_evidence_integrity.py::test_failed_service_inventory_cannot_certify_partial_output` |
| container_lookup_exit | `tests/test_deploy_evidence_integrity.py::test_failed_container_lookup_cannot_use_its_stdout` |
| service_identifier_validation | `tests/test_deploy_evidence_integrity.py::test_duplicate_or_malformed_service_list_is_refused[bad name PRIVATE_DIAGNOSTIC_MUST_NOT_APPEAR]` |
| unique_service_names | `tests/test_deploy_evidence_integrity.py::test_duplicate_or_malformed_service_list_is_refused[dashboard]` |
| complete_container_list | `tests/test_deploy_evidence_integrity.py::test_multiple_containers_cannot_certify_only_the_first` |
| container_identity_validation | `tests/test_deploy_evidence_integrity.py::test_multiple_containers_cannot_certify_only_the_first` |
| restart_counter_continuity | `tests/test_deploy_evidence_integrity.py::test_restart_counter_decrease_does_not_certify_continuity` |

Exact reports: `docs/evidence/pilot-deploy-evidence-suite.json` and
`docs/evidence/pilot-deploy-evidence-regressions.json`.

## External references and remaining requirements

Docker documents formatted inspection output and listing all container IDs. These
API references inform parsing; they do not establish the actual host's health:
https://docs.docker.com/reference/cli/docker/inspect/
https://docs.docker.com/reference/cli/docker/compose/ps/

Require exact published-head CI after pushing. Keep PR #133 draft and unmerged.
The portfolio-risk inventory remains 51 planned / 0 executed / 0 certified, and the
proposed Kite adapter remains absent. No blocked operation was retried or bypassed.
Usable real history, covered-exit and target-host acceptance remain open. Item 3
must wait until the owner merges Item 2; MCX admission is not advanced here.
