# Bounded consensus output profile and fail-closed completion

## Current evidence and scope

The owner supplied five matched AWS decision-proof summaries. Each requested 1200
output tokens, recorded 1200, and ended in invalid_schema/abstention. The deployed
version did not save stop_reason. This strongly supports testing response truncation,
but does not prove the stop reason or exact malformed field in those past responses.

This is an extension of existing PR #153, not a competing fix. It reuses that PR's
response metadata, finite validation codes and isolated exact-request probe. No
credentials, paid model request, running AWS configuration, ledger or service are
changed by this development work. The selected source is parent 296bc9a on main
bf3812088. No owner merge or deployment is performed.

## Output profile

`PRAMANA_CONSENSUS_MAX_TOKENS` is forwarded only to the engine. Its default remains
1200 for byte-identical legacy requests. An explicit value must be a canonical integer
from 1200 through 4096. Invalid, boolean, fractional, out-of-range, non-ASCII and
ambiguous values refuse before SDK construction, without echoing the supplied value.
The optional constructor argument follows the same bounds and overrides the environment.

The proposed operator-selected recovery profile is:

```dotenv
PRAMANA_CONSENSUS_MAX_TOKENS=4096
```

This is an experimental per-response ceiling, not a claim that any specific real
response will fit, nor permission to increase daily spending. It is not automatically
selected in existing deployments. Compose forwards the actual setting; changing an
unforwarded host variable cannot be mistaken for activating it in a running container.
A change in this setting changes the runtime configuration fingerprint and every
request/proof hash, rather than silently reusing an old configuration identity.

A larger profile asks for a one-sentence summary and at most three short evidence
items per list. These are generation instructions; output is never sliced, repaired,
coerced, supplied with invented missing fields or accepted merely for obeying length
advice. The original field/numeric/enum schema and parser are unchanged. Model,
thinking mode, timeout, SDK retry behavior and headline-scoring settings are unchanged.

Daily call and recorded-token budgets keep their existing defaults and operator
values. No new retry is added, and output tokens are recorded before invalid-response
refusal. A larger individual ceiling can use remaining daily allowance sooner and
can take longer; current timeout and cost-accounting limitations remain in force.

## Completion and diagnostic safety

- Explicit max_tokens, refusal, context-limit and paused responses remain unusable,
  even when a valid-looking trading object is present.
- Unexpected stop values and non-tool termination refuse. Real SDK responses with
  missing termination evidence refuse. Legacy injected test objects may retain a
  missing stop reason as null; that compatibility is not external completion evidence.
- Exactly one matching tool with an object input and the original strict validation
  is required. Atlas retains its existing non-actionable hold on schema failures.
- Stop/block metadata is bounded and allowlisted. Validation exceptions no longer
  embed arbitrary unknown field names; durable diagnostics use finite failure codes.

## Verification

The initial 40 new tests yielded 39 failures / 1 pass on unchanged PR parent, with no
collection errors or skips. With the patch those 40 passed; three additional boundary
checks were then added. The final focused combination passes 111 cases: 43 new behavior,
28 paired guard-removal, 21 retained completion diagnostics, 17 retained probe, and
2 original Anthropic swarm tests. All 28 guard controls pass; changed disposable copies
fail selected tests with no collection errors/skips; copies restore and original
hashes match. Inventory: `docs/evidence/consensus-output-guard-inventory.json`.

The fresh unchanged-parent full Mac run passes 3020 ordinary tests plus 14 passing
subtests, two dependency warnings, no failures/errors/skips. Final candidate full-suite
and exact-head CI results are recorded on the PR, not inferred from focused tests.
No pre-existing test or CI assertion is removed, edited, skipped or marked expected
failure. The requested /usr/local/bin/ruff is absent; lint runs through the absolute
existing project virtual-environment interpreter. An optional broad remote inspection
was tool-blocked before execution; no permission or safety setting was altered.

The five-symbol fixture emulates truncated 1200-token envelopes followed by complete
responses under 4096. It verifies request wiring and refusal/acceptance behavior, not
real model completion, actual account trading or profitability. Earlier real synthetic
controls in the parent PR did not reproduce AWS; this extension makes no new paid call.

## Owner rollout and acceptance

Keep PR review and merge separate from running-host rollout. Preserve current tokens,
Kite/daily history, five-name directives, persistent data and paper mode. Do not widen
PR #144, raise daily budgets, clear a halt or disable the schema guard to force activity.
After owner review/merge, deliberately select the proposed profile in the private root
.env and use the reviewed deployment procedure with its existing session-window rules.
Do not change the running container's Python files in place.

The operational acceptance evidence must come from the actual daemon: the new release
and output limit, fresh quotes, required data-ready risk gates, reconciliation and
responsive protection; then exact post-rollout decision proofs with stop_reason=tool_use,
status=completed and one validated tool. A legitimate NEUTRAL/abstention is acceptable;
a BUY/fill is not a required test outcome. Continued max_tokens/refusal/timeout remains
an explicit failure, not justification for an unbounded ceiling or automatic retries.

The existing one-request probe intentionally refuses a saved request that differs
from its installed adapter. It must not label an old 1200-profile replay as a test of
the new 4096 profile. Actual model latency, billed cost and response completion remain
unverified until a bounded operator-authorized real check or post-rollout cycle.

## Primary references

https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons
https://platform.claude.com/docs/en/agents-and-tools/tool-use/strict-tool-use

Anthropic documents max_tokens and incomplete tool-use responses. This patch uses
that termination signal and retains local validation; it does not introduce a new
strict-tool API option or claim schema compliance can bypass response truncation.
