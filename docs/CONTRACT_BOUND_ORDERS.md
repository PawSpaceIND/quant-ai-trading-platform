# Contract-bound order identity prerequisite

This change is a **paper-only identity prerequisite**, not MCX pilot admission. The default
pilot still accepts NSE/INR cash equity and ETFs only. No real broker writes, live-money
configuration changes, sourced fee rates or external evidence are supplied by this change.

## Implemented boundary

`InstrumentBoundOrderIntent` is an opt-in subtype of the existing `OrderIntent`. It takes a
validated, defensive snapshot of `Instrument`: symbol, market, asset class, currency,
exchange, expiry, lot size, tick size, underlying and supplied metadata. Metadata is copied
and frozen too; mutating the original catalog object cannot alter an existing decision.
The snapshot remains compatible with `dataclasses.asdict` and `dataclasses.replace`.

The paper broker persists canonical identity JSON on each bound fill and open position,
and records configured identities in `pilot_scope.instrument_identities`. Optional schema
columns migrate forward; legacy cash records remain explicitly unbound rather than being
retrospectively assigned an invented contract. Existing cash-order idempotency bytes are
unchanged. Bound-order keys additionally include the canonical contract digest.

A held bound position cannot be relabeled or closed with a plain order. Protective exits
construct a bound order from the position's own durable snapshot; they do **not** resolve
contracts through a global catalog at fill time. Portfolio marking also prefers that saved
snapshot over any supplied catalog resolver. Malformed position identity is reported in
protection coverage; a corrupt holding does not suppress exits for unrelated valid holdings.

Bound orders run existing expiry/rollover and lot/reference-tick checks before pricing.
Dated contracts also require the resulting fill to be on tick; this module does not invent
a tick-rounded price. Expired contracts refuse both directions and require reconciliation,
not a fictional post-expiry trade. No settlement or rollover is automatically performed.

Independent paper-account replay checks each fill's identity against its row and the
identity of the open episode, then compares the derived identity with the current position.
This detects identity drift without rewriting either source. It does not authenticate a
coordinated alteration of all records or certify external contract-master correctness.

## Verification

Baseline main: `356cea4576bfd75bfa5321b686c9df7df3a1b08f`.
Local Python: 1,422 passed, 13 failed, 14 subtests passed (1,435 collected test cases).
All 13 failures are the pre-existing `test_deploy_pilot_host.py` Bash-3/BSD-tooling defects,
not waived tests. Source and tests pass Ruff. Focused identity/protection/reconciliation/
pilot run: 86 passed; repaired legacy-stub/fixture/overnight set: 82 passed.
Commands used the candidate worktree's `src` explicitly on `PYTHONPATH`.
UI verification: TypeScript passed, 151 tests passed, and Next.js production build passed.

Six mutations were run in disposable source copies. Each made its named behavioral test
fail: legacy idempotency drift; caller-owned mutable metadata; SQLite row-value membership;
unbound protective exit; omitted expiry guard; omitted identity-replay comparison.
The production worktree was not mutated for those checks. Raw logs and JUnit output are in
`/private/tmp/quant-ai-contract-orders-evidence-20260916` on the development Mac.

## Remaining acceptance gates

| Requirement | Status after this prerequisite |
| --- | --- |
| Immutable identity on opt-in orders, positions, fills and configured scope | Implemented and regression-tested |
| Legacy cash keys and deterministic protective exits | Preserved and regression-tested |
| Bound expiry, lot and tick refusal | Implemented; inherited rollover window is calendar-day-based, not a certified exchange-session calendar |
| Identity coherence in paper-account replay | Implemented; internal consistency only |
| Proposal, Warden, OMS, restart and strategy evidence carrying the same contract end to end | Part 4 integration still required; default cash flow is intentionally unchanged |
| Exact-identity linkage to margin and fee sources | Part 4 integration still required |
| MCX admission, non-agri classification, fresh margin and real contract-note reconciliation | Not opened or externally validated here |
| Daily variation settlement, margin revisions, delivery/expiry settlement and roll execution | Not implemented by this prerequisite |
| Multi-venue simultaneous holdings with the same symbol/class key | Not claimed; identity substitution on an existing key refuses |
| Human acceptance and target-host real-session burn-in | Not established by local unit tests or CI |

Do not treat a green integration build as a passed MCX acceptance gate.
