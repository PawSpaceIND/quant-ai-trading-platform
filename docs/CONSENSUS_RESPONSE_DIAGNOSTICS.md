# Claude consensus completion and isolated replay diagnostics

## Scope and current evidence

Prepared against main `bf3812088e4308eb5372203669d9e07cd8e74562`.
The owner supplied AWS evidence of current responses with `invalid_schema`,
returned model IDs and response IDs. That confirms a response was received,
not the precise validation failure. Existing evidence did not retain stop reasons.

This change does not claim to fix the observed AWS incident before replay evidence
identifies its cause. It changes no model, request schema, confidence threshold,
thinking setting, token limit, timeout, daily budget or deployment default.
No AWS credential action, service restart, order or model approval is performed.

## Implemented repair

Consensus provenance now retains a bounded stop reason, block-type summary and
matching-tool count. Validation failures retain a fixed `failure_code`, never
arbitrary field names, raw provider text, tool inputs or exception contents.

Explicit truncation (`max_tokens`), refusal, context exhaustion and incomplete
turns cannot be accepted even when a complete-looking BUY payload is present.
Exactly one consensus tool is required: missing, wrong or multiple tool blocks
refuse. Existing strict field/numeric/enum validation is retained. Spent tokens
are accounted before refusal; no retry is added. Atlas preserves the existing
NEUTRAL/zero-confidence abstention and carries the new diagnosis into its proof.

Missing stop metadata on legacy/injected response objects is recorded as null;
this patch does not falsely add provider completion evidence to older fixtures.
A new provider response with an unrecognized stop label is recorded as such.

## One-call production-host diagnostic without restarting the engine

`scripts/probe_consensus_response.py` can be executed in a separate process inside
an existing engine container. It is compatible with the reviewed main adapter;
the new provenance changes do not need to be deployed just to run this probe.
After reviewing the script, use the approved copy as stdin:

```sh
docker exec -i deploy-pramana-ghost-1 python - --confirm-paid-call \
  < scripts/probe_consensus_response.py
```

This requires the actual container's explicit `TRADING_LIVE_MONEY_ACTIVE=false`,
configured Anthropic key and existing enabled AI budget. The latest matching
saved request among 100 bounded proof files must have a matching stored hash.
The installed adapter must construct exactly that same request before it is sent.
A changed model, schema, tool definition, token cap or other request setting refuses.

At most one SDK request is attempted, with SDK retries disabled and the existing
30-second adapter timeout. The destination is explicitly the official Anthropic
API, independent of any alternate endpoint environment variable. The existing
consensus budget records the attempt/spend. Output is metadata only: stop reason,
usage, block types, known field types, unknown-field count and validation status.
It does not print the saved prompt, returned rationale, credentials or tool values.

No broker/runtime is instantiated, no signal is dispatched, no decision proof is
published, and the paper ledger is not opened. A success is a protocol replay of
historical supplied context, NOT a new investment decision, source-freshness proof,
strategy qualification or evidence that the background engine has recovered.

## Evidence so far

- Existing focused baseline: 70 passed.
- 21 new completion/diagnostic tests failed on unchanged source; after repair they
  pass, including valid-looking truncated BUY refusal and preserved budget use.
- Focused final run: 117 passed, covering the 21 new adapter cases, 17 probe cases
  and existing consensus, Atlas, budget, provenance, context and headline tests.
- Ruff on every changed Python file and `git diff --check` passed.
- Full-suite and exact-head CI results belong in the PR; do not infer them here.

Two separately bounded REAL Anthropic calls from an isolated Mac process used the
UNCHANGED main adapter and synthetic inputs. A simple diagnostic completed with
`stop_reason=tool_use`, 714 output tokens; a richer existing evidence-context
fixture completed with `stop_reason=tool_use`, 990 output tokens. Both retained
`max_tokens=1200`, the current model and default thinking behavior. No broker or
runtime was started. These controls did not reproduce the AWS incident and are
not AWS acceptance or evidence of trading skill. No conclusion that changing
thinking or increasing the output cap fixes AWS is justified by those controls.

The primary Mac checkout and its 21 pre-existing changes were preserved. All
source/test work is isolated in a separate clone; no pre-existing test is edited.

## Official references

- https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons
- https://platform.claude.com/docs/en/models/sonnet-5/whats-new-sonnet-5
- https://platform.claude.com/docs/en/agents-and-tools/tool-use/strict-tool-use

Sonnet 5 adaptive thinking defaults and shared output limits are relevant leads,
not a substitute for the actual rejected response's stop reason and field shape.
