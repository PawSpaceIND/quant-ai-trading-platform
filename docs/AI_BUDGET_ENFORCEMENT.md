# AI budget enforcement: aggregate admission contract

This document describes the paper runtime's source-level AI admission controls. It does
not claim a provider invoice or currency-denominated spending ceiling and does not change
credentials, model selection, watchlists, trading limits or live-money authority.

## Daily ceilings

`PRAMANA_AI_DAILY_CALL_LIMIT` defaults to 500 and
`PRAMANA_AI_DAILY_TOKEN_LIMIT` defaults to 2,000,000. Existing positive
operator-selected values still take precedence.

The configured values now apply in two places:

1. each named AI scope (`consensus`, `headline_sentiment`); and
2. one account-wide aggregate ledger shared by all scopes.

The aggregate call counter means 500 is at most 500 admitted AI calls across all scopes,
not 500 calls independently for each scope.

The counter day remains UTC. Restarting a process does not clear that day's rows.

## Pre-request token reservation

Production Anthropic calls calculate a conservative allowance before network I/O. The
allowance consists of the UTF-8 size of the complete structured request, the requested
maximum output tokens and fixed provider/tool framing headroom. Admission atomically
checks both the scope and aggregate call/token headroom and records the reservation before
the request leaves the process.

When valid provider usage is returned, recorded input/output usage is added and the
matching reservation is released. If a timeout, provider failure or malformed/missing
usage prevents reliable reconciliation, the reservation remains consumed for the UTC day.
The safe failure mode is therefore reduced AI availability, not reopened spending
headroom.

Provider-reported token usage remains the accounting truth after a completed call.
Reservations are deliberately conservative admission units; they are not a provider bill,
a rupee/dollar cost estimate or proof of the provider's final invoiced token categories.

## Durable upgrade behavior

Existing SQLite databases are migrated in place by adding the reservation column if
needed and creating the aggregate table. Existing per-scope calls and recorded tokens are
summed into a missing aggregate day row exactly once. Upgrading therefore cannot reset
already-consumed daily headroom.

## Concurrency and failure behavior

Aggregate and scope admission are committed in one `BEGIN IMMEDIATE` transaction. If
either ceiling refuses, the transaction rolls back and no call is admitted. Multiple
threads/processes sharing the SQLite file therefore cannot each consume the same remaining
aggregate call slot.

SQLite errors fail closed. Invalid reservation values are rejected. Unknown usage does
not release a reservation.

## What this control does not claim

- It does not convert tokens into currency or reproduce the provider invoice.
- It does not prove that provider-side retries, caching categories or billing adjustments
  equal the local counters.
- It does not authorize live-money trading.
- It does not require the AI to trade; budget exhaustion degrades consensus to the
  existing neutral/preserve-capital path.

## Implementation and verification

- `src/quant_ai/llm/budget.py`: durable scope + aggregate ledger, atomic admission,
  reservation reconciliation and migration.
- `src/quant_ai/llm/anthropic_client.py`: conservative pre-request reservations for
  consensus and headline scoring.
- `tests/test_ai_budget.py`: admission, concurrency, provider-call and reconciliation
  coverage.
- `tests/test_ai_budget_enforcement_contract.py`: aggregate cross-scope, token-headroom,
  unknown-usage and legacy-migration acceptance.
