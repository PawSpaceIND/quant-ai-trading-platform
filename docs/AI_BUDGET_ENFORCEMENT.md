# Existing AI budget enforcement: operator contract

This document describes current implementation, not a new spending policy or a
strict account-wide billing guarantee. It addresses the immediate clarification in
issue #162. It does not activate a model, raise a limit, widen a watchlist, clear a
halt, change credentials, or authorize live-money execution.

## What the existing settings mean

`PRAMANA_AI_DAILY_CALL_LIMIT` defaults to 500 and
`PRAMANA_AI_DAILY_TOKEN_LIMIT` defaults to 2,000,000. Existing operator-selected
values take precedence. Both limits apply **independently to each scope** in the
SQLite budget ledger: `consensus` and `headline_sentiment`.

The day is a **UTC calendar day**, not the exchange session or the local calendar
day. The UTC date changes at 05:30 Asia/Kolkata. Rows remain in the durable database;
restarting a process does not erase that day's used admissions or recorded tokens.

The call counter measures adapter admissions. It is not a guaranteed count of
provider HTTP attempts, since transport retry behavior is separate. The token
counter adds reported `input_tokens` and `output_tokens` after a response arrives.
It does not incorporate cache-creation/cache-read fields into this counter and is
not a currency ledger or a complete provider billing meter. Missing or malformed
usage contributes zero tokens, but does not refund the already admitted call.

`reserve(scope)` atomically checks that scope's existing call and token totals and
increments its call counter. A database error refuses admission. It does **not**
reserve the next request's potential token consumption before sending the request.
Consequently a request admitted just below the token threshold can take the total
above it. Later reservations then refuse. Concurrent in-flight admissions and
incomplete usage reporting need separate treatment in any future hard-ceiling design.

## Executable synthetic examples

With a 100-token threshold and 99 already recorded tokens, a new call is admitted.
If that response reports another 40 tokens, the recorded total becomes 139 and the
following admission refuses. These are synthetic token counts, not actual account
usage or a cost estimate. Recording all 40 is correct accounting; silently clipping
the ledger to 100 would conceal consumed usage rather than enforce a ceiling.

With a per-scope call limit of one, one `consensus` admission and one
`headline_sentiment` admission both succeed. That is two aggregate admissions,
not proof of a one-call account-wide maximum. Each scope then refuses its next call.

The matching executable characterizations are in
`tests/test_ai_budget_enforcement_contract.py`. They describe existing behavior;
they do not certify it as a hard aggregate limit or waive issue #162.

## Response ceilings are a different control

The consensus default is 1,200 output tokens. The separately reviewed explicit
`PRAMANA_CONSENSUS_MAX_TOKENS` choice can reach 4,096; merging code does not select it.
The headline response ceiling remains 1,000. These per-response output allowances
are not input-token reservations, daily aggregate limits or currency limits.
Increasing an allowed response size can increase the overshoot of a post-usage
threshold. Existing configured daily values must not be raised to hide exhaustion.

## Operator acceptance before a rollout

Keep the paper-only setting and current positive budget values unchanged. Zero or
negative configured call/token limits explicitly disable this implementation's
budget; they must not be used as an exhaustion workaround. Preserve the budget
SQLite database, ledger and decision evidence when restarting or deploying.

Review scope-specific calls, recorded tokens, remaining calls/tokens and exhausted
status without printing credentials. Do not advertise these values as a hard
account-wide monetary cap. Actual billed spend requires separate provider evidence.
When reported usage is valid and the database write succeeds, rejection of the
response still records its consumed input/output tokens. A logged write failure
is not proof that consumption was persisted. Invalid responses must never be
promoted into model evidence to make the system look active.

A hard aggregate ceiling remains a separate policy and engineering decision. Its
acceptance must define the aggregate/per-scope relationship, conservative reservation
of input/output allowances, unknown usage and timeout/retry treatment, concurrency,
UTC-day rollover and durable reconciliation. Until implemented and independently
verified, no strict aggregate or currency-ceiling claim is justified.

## Implementation sources

- `src/quant_ai/llm/budget.py`: `reserve`, `record`, `status`, `current_day`, and
  `budget_from_env`.
- `src/quant_ai/llm/anthropic_client.py`: scope names, request ceilings, usage recording
  and unchanged transport construction.
- `deploy/docker-compose.yml`: forwarding of operator settings to the engine.

No statutory rates, model prices or new policy values are introduced here.
