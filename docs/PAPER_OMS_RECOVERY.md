# Reviewed recovery of committed paper fills

Continuation of draft PR #126 from `8bd646f97a8e145c7b2718c0e9d0c798c97131d8`.
The previously interrupted recovery work is retained and reviewed; current main at
`13bbdec81fe59332513260b746d51ec75bcf565d` is included without removing the
research-publisher changes or the blocking institutional-risk acceptance job.

## Problem and boundary

An existing bound-cash swarm submission can commit its paper fill before an error
leaves its OMS order SUBMITTED or SUBMISSION_UNCERTAIN. Repeating submission would
be unsafe; recording a rejection would misdescribe a committed trade.
`PaperOmsRecovery` provides explicit inspection and reviewed OMS reconciliation.
It never submits, cancels or rejects an order, changes the paper ledger, posts
accounting, clears a halt, activates a model or enables live execution.

Scope is exact local PaperBrokerService and DurableOms components, the persisted
bound_v1 configuration, and NSE INR EQUITY/ETF instruments. External broker-bound
orders, unbound history, other segments/currencies and partial or conflicting
terminal OMS outcomes refuse. Missing receipts never authorize a retry.

## Evidence required

Inspection reconstructs the saved complete order intent and decision identity,
then obtains the broker's immutable committed receipt through its idempotency key.
The receipt, fill economics, contract snapshot, risk approval, matched runtime
manifest, tenant and pinned OMS storage must agree. It checks execution timestamps
and other OMS claims on the same broker fill. A stored recovery audit may not be
used at an inspection time preceding that audit's recorded time.

The returned plan has READY, MATCHED or BLOCKED status and a canonical payload
hash. Recovery requires the exact inspected hash, a named reviewer and an already
persisted halt. It rechecks evidence under the broker/OMS locks and database
snapshots. A changed reviewed plan refuses; there is no automatic approval.

## Atomic OMS update and restart

Only the absent OMS fill is reconstructed. Its event records the reviewed plan,
receipt hash, reviewer and recovery time. An append-only recovery audit and OMS
schema version 2 commit in the same OMS transaction. Missing or mismatched audits
are rejected by ordinary OMS verification as well as recovery inspection.
A failure before commit rolls back fill, audit and schema changes together.
An abrupt exit after commit retains one fill and one audit; repeating recovery
with the original reviewed plan is idempotent. The paper database stays unchanged,
including when a protective exit has already closed the original position.

Fresh OMS databases still begin at version 1; successful recovery requires version
2. Older version-1-only readers refuse the recovered database on restart rather
than interpreting its history without the new audit semantics. This is a
compatibility guard, not a general migration, downgrade or rollback utility.

## Verification

The recovery test file contains 41 synthetic cases. The focused recovery,
runtime-contract, durable-OMS and broker-reconciliation set passes 95 cases.
All tests use temporary accounts and receipts; no real account is recovered.
Cases cover committed buys and sells, no-receipt uncertainty, stale reviewed
plans, conflicting states, missing/altered approval or release evidence, wrong
tenants, preserved halts, recovery after independent exits, competing connections,
audit rollback and abrupt subprocess exits before audit or after commit. Two
additional cases reproduce inspection/recovery before the audit timestamp.
Final combined full-suite, mutation and exact-head CI evidence is recorded in #126.

## Limits and remaining work

This is an explicit Python service API, not an unattended daemon repair loop or
an authenticated operator console. Reviewer text and hashes bind local declarations;
they are not authentication or proof of genuine market data. MATCHED means this
one paper order agrees, not that the whole account or platform is launch-ready.
The existing durable halt is deliberately retained for separate reviewed release.
No audit evidence or old approvals are fabricated when source records are absent.

The two databases are not a distributed transaction: only OMS is changed, using
a consistent broker snapshot under local locks. Advisory/local synchronization is
not cross-host fencing, and external writers or hardware failure need separate
operational handling. Separate accounting and full institutional-daemon assembly
are not introduced here. General legacy migration, rollback and review identity
remain open, alongside segment/settlement and authentic operational acceptance.
The unchanged 15-failure institutional risk-acceptance gate remains a blocker.
No real broker call, actual account recovery or deployment was performed.

## Resumed certification findings

The first resumed combined run reported 2164 normal-suite passes and one failure:
the two-connection recovery case exposed a timestamp sampled before lock acquisition.
One caller could wait behind a newer committed audit and then reject that audit as
future relative to its stale invocation-time clock. Two deterministic regressions
reproduce the ordering without sleeps. Implicit time is now sampled after database
serialization; explicitly supplied historical time stays fixed and still rejects
future audit evidence. The finished recovery file contains 41 cases and the focused
surrounding set passes 95. The failed run is retained, not erased or reclassified.
