# Explicit immediate-bridge accounting recovery

Continues PR #146 from `9778400aa3ff8006438e5c3df8655b47f6802d49`.
This adds a public component recovery operation to the existing immediate institutional
swarm route. It is not an HTTP endpoint, operator authentication, automatic restart,
scheduled execution, position-risk release or live-money authorization.

## Operation and returned meaning

`InstitutionalSwarmPaperTradingService.reconcile_program(program_id, tenant_id=...)`
loads the complete saved request/plan, validates that it originated in this bound
NSE/INR cash IMMEDIATE bridge, and uses the existing coordinator to reconcile the
original broker receipt, OMS record and double-entry accounting. It never calls
`execute_due` or either direct/institutional submission path.

The returned immutable InstitutionalRecoveryReport identifies the selected programme,
its recorded state, reconciliation stage/reason, original committed broker order IDs,
sequences reconciled by this call and original input-source revision.
`execution_authorized` is always false. These are historical order references, not
new fills to publish again as trade notifications. Recovery does not regenerate XAI
file projections or claim a new decision was made.

Live market, calibration, factor, strategy-exposure and liquidity providers are not
invoked. Missing current preflight/snapshot hooks do not block historical bookkeeping.
The selected broker, OMS, programme/accounting stores and existing scope bindings
still have to match. Current risk-policy changes and retained entry halts do not
authorize another trade and do not make an already committed fill unrecoverable.

## Evidence and refusal

The full saved decision trace must accompany the bridge context. Every extant selected
receipt is checked before reconstruction; its trace must exactly match the immutable
source trace, with only the recorded broker order ID added. This trace correspondence
also applies inside the coordinator's ordinary receipt-recovery path, not just the
new wrapper. A missing receipt is not permission to retry or clear a dispatch claim.

The wrapper does not trust COMPLETE alone. Completed slice states must agree with
programme completion, and each existing/recovered EXECUTED fill must have matching
ledger-derived trade and cash-fee postings. The posting model reuses the existing
ProtectiveExitAccounting replay, including prior holdings needed for sale cost basis.
A missing or economically inconsistent posting for an already EXECUTED programme
refuses; this operation does not silently repair a rewritten completed journal.

The coordinator still owns crash recovery and journal transaction boundaries. Failure
can leave an original fill awaiting accounting; subsequent explicit reconciliation
uses that same receipt. Independent protective exits remain outside the bridge. A
position closed independently before recovery stays closed, and its historical
accounting is reconciled without reopening or releasing its risk reservation.

## Verification scope

21 new synthetic cases cover committed-fill reconstruction, missing receipts,
changed traces, tenant/store refusal, absent providers, changed current policy,
concurrent calls on one service, actual store reopen, accounting failures, deleted
completed postings, cash fees, false completion flags, independent exits and three
abrupt recovery-process termination boundaries. The two initial API specifications
failed because no public wrapper existed. Two later development tests reproduced
missing completed-posting/state checks and then passed after adding them. They are
retained as intermediate evidence, not relabelled as a previously certified run.

No pre-existing test or acceptance assertion is modified. Final frozen-tree results
and exact CI/commit evidence are recorded on #146. Synthetic amounts do not constitute
recommended allocation, authenticated market data or evidence of profitable trading.

## Remaining boundaries

This checks the selected programme and its retained local evidence, not a consistent
snapshot of every concurrent account or independently authenticated source truth.
It is not a distributed recovery lock, global account reconciliation, missing-source
repair, signed approval chain or protection against privileged rewriting of all
stores. Concurrent separate coordinators still rely on existing durable OMS/programme
idempotency and must surface contention rather than dispatch a replacement.

An operator-facing authenticated review/confirmation workflow, automatic recovery
policy, scheduled TWAP/VWAP/POV lifecycle, environment-only deployment selection,
qualified sources and target-host acceptance remain open. M01 position-linked risk
release remains unimplemented; no blocked release operation is retried here. Existing
M02 lineage and the remaining M03/12-milestone requirements are not marked complete.
