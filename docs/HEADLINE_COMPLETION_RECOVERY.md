# Headline completion and evidence acceptance

## Scope

Follow-up to audit issue #161, based on main
`8be4c17717e3f43a3f8b7a81877a39b91fc5ab16` after merged consensus PR #153.
The candidate was subsequently integrated onto current main
`f9989735f2298e5edc4b62d832bcc4f7adcad8fa` (approval/health changes with no
file overlap). Only the headline response-handling method changes. The consensus method, existing
strict parsers, headline scorer/cache, budget policy, provider requests, deployment
configuration, and all pre-existing tests/CI files remain unchanged.

## Response contract

The adapter reuses the bounded/allowlisted response-shape projection and records
actual usage before rejecting an unusable reply. It returns empty scores with
`status=invalid_schema` and a finite `failure_code` when termination is truncated,
refused, paused, context-exhausted, unrecognized or not a completed tool call.
Missing real-SDK termination evidence also refuses. Exactly one client tool block
with the expected headline tool name and object input is required, followed by the
existing strict headline parser. Failed replies carry no accepted-payload hash.

The existing scorer consequently uses its labelled keyword fallback and does not
cache the rejected model evidence. A later normal cadence call can recover and be
cached; this is not a new immediate retry. Model, headline output cap1000, timeout,
SDK retry behavior and both daily-budget settings are unchanged.

Legacy explicitly injected fixtures can retain absent termination as null; their
`transport=injected_client` label remains visible. This compatibility is not proof
of a real provider completing a response. Real-SDK cases in the new tests replace
only the SDK factory with an offline transport, preserving the real transport label.

Unknown metadata and schema failures are redacted; provider text, arbitrary field
names and raw exception messages are not copied into failure diagnostics. Malformed
metadata safely falls back after usage accounting rather than escaping into cadence.

## Verification

The final58 new behavioral cases were run on an unchanged main source archive:
**49 failed / 9 passed**, no errors/skips. On the patch all58 pass. Coverage includes
adapter completion/envelope validation, strict payload checks, the scorer/cache
boundary, usage-before-rejection, exhausted-budget refusal, privacy and unchanged
request identity. The final focused group is **280 passed**, no failures/errors/skips.

The new18 paired guard-removal checks reuse the existing consensus mutation runner
without editing it. Each unchanged control passed and each deliberately changed
copy failed its designated assertion; all copies restored and original hashes match.
The prior28 consensus guard checks remain green: **46 pairs total**. These are offline
synthetic-input checks, not real-provider, target-host or live-money acceptance.

Full-suite and exact-head CI results are recorded on the PR; these focused counts
must not be added to full-suite totals. Ruff on src/tests and whitespace checks pass.
Local evidence is retained outside the checkout so source/release identity is stable.

## Guard-to-test inventory

Machine-readable source changes: `docs/evidence/headline-completion-guards.json`.

| Guard or diagnostic removed | Test detecting the change |
|---|---|
| metadata_projection | `tests/test_headline_completion.py::test_complete_reply_has_real_completion_and_payload_identity` |
| malformed_metadata_refusal | `tests/test_headline_completion.py::test_malformed_metadata_refuses_after_usage_recording` |
| headline_tool_count | `tests/test_headline_completion.py::test_complete_reply_has_real_completion_and_payload_identity` |
| diagnose_max_tokens | `tests/test_headline_completion.py::test_sdk_termination_must_be_completed_tool[truncated]` |
| diagnose_refusal | `tests/test_headline_completion.py::test_sdk_termination_must_be_completed_tool[refused]` |
| diagnose_pause_turn | `tests/test_headline_completion.py::test_sdk_termination_must_be_completed_tool[paused]` |
| diagnose_model_context_window_exceeded | `tests/test_headline_completion.py::test_sdk_termination_must_be_completed_tool[context]` |
| explicit_stop_must_complete | `tests/test_headline_completion.py::test_sdk_termination_must_be_completed_tool[end_turn]` |
| sdk_requires_termination | `tests/test_headline_completion.py::test_sdk_termination_must_be_completed_tool[missing]` |
| missing_headline_tool | `tests/test_headline_completion.py::test_envelope_requires_one_correct_object_tool[missing]` |
| single_headline_tool | `tests/test_headline_completion.py::test_envelope_requires_one_correct_object_tool[duplicate]` |
| expected_headline_tool | `tests/test_headline_completion.py::test_envelope_requires_one_correct_object_tool[wrong]` |
| object_before_parser | `tests/test_headline_completion.py::test_parser_is_not_called_on_nonobject_input` |
| strict_headline_parser | `tests/test_headline_completion.py::test_adapter_validates_existing_strict_schema_before_completion[range]` |
| redacted_schema_failure | `tests/test_headline_completion.py::test_schema_error_does_not_echo_provider_values` |
| invalid_status | `tests/test_headline_completion.py::test_sdk_termination_must_be_completed_tool[truncated]` |
| usage_before_rejection | `tests/test_headline_completion.py::test_spent_usage_is_recorded_before_rejection_without_extra_request` |
| budget_before_request | `tests/test_headline_completion.py::test_budget_exhaustion_prevents_the_first_sdk_request` |

## Operational boundaries

No credentials, paid model requests, AWS operations, service restarts, live orders or
budget increases were required for this fix. Issue #162's existing per-scope, post-use
spending threshold is unchanged. This change does not activate the separate4096
consensus profile or prove that AWS has recovered. Existing permitted-window rollout,
fresh-data, risk, reconciliation and protection acceptance remain required.

## Primary provider reference

https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons

Provider termination is distinct from local schema validation; both are required for
real-provider headline evidence. No automatic provider fallback or retry is added.
