# Broker-owned admission witnesses for pending paper reservations

Continues existing draft PR #137 from `7b5d186508c232ea192194b066f6016fd4eb3e64`.
This is an additional refusal/integrity boundary within M01. It does not implement
position-linked capacity release, change account risk limits or enable a daemon.

## Reproduced gap

The committed-entry check requires a recorded purchase. Before any fill, restoring
an older execution journal can erase a newer pending reservation while retaining
the same UUID, paths and shared policy. Three regression tests on the published
base reproduced readmission of capacity, successful pairing and final broker
submission despite the lost pending programme. They now require refusal.

## Independent record

A new explicit shared-account binding uses
`pramana.paper_shared_risk_binding.v2`. The broker ledger retains an append-only
`paper_shared_risk_admission_witnesses` table, created only during explicit setup.
Each admitted parent has one witness binding the tenant, programme, journal UUID,
broker-pin hash, reservation hash, complete parent/authority hashes, plan hash,
decision identity, creation time and ordered schedule hash. Completed or cancelled
programmes retain their original witness; this record itself frees no capacity.

Normal pairing, coordinator admission, final broker entry validation and offline
pairing compare both inventories and exact immutable content. An older journal
cannot hide a missing reservation merely because no BUY exists yet. Missing tables,
rows, modified schedules or inconsistent history refuse rather than being repaired
by inspection. Checks preserve the existing committed-entry and authority errors.

## Commit and failure ordering

The execution journal writer transaction is held while the parent and reservation
are prepared. Only that exact still-uncommitted programme may temporarily lack its
witness on the internal append path. The broker witness commits before the journal
transaction can finish. A crash between those commits leaves a witness referring
to absent journal history: further entries hold. This is NOT an atomic transaction
across the two databases, and the hold is not automatically cleared or retried.

A new witness cannot be backfilled for an already claimed programme. Admission
rejection does not append a witness. Exact-decision retries retain one record, and
cooperating coordinators sharing one journal remain serialized. Original
never-claimed cancellation remains unchanged and does not delete witnesses.

## Legacy, restoration and exits

Old v1 pins remain readable, including offline history verification. Runtime use
of a v1 shared pin holds for reviewed migration; the code does not invent admission
witnesses for its existing programmes. No actual account has been migrated here.
The optional table is absent on ordinary, unselected paper accounts.

Offline path exceptions never exempt v2 pending-history checks. Backups preserve
witness bytes and pending discrepancies without starting execution. Covered sales,
independent protective exits and their accounting remain available despite a lost
pending journal record. Retaining a witness does not post or repair accounting.

The read bounds are 100,000 reservations/witnesses and 10,000 slices per programme.
Exceeding either limit refuses; no truncated portfolio is described as complete.

## Verification and remaining boundaries

37 new synthetic cases cover the three reproduced pre-fill rollback paths, same
identity retries, competing coordinators, cancellation, exact history and schedule
changes, missing/corrupt witnesses, old v1 pins, restored backups and independent
protection. Three real subprocess exits cover failure before the witness, after
its commit but before journal commit, and after both commits. They preserve the
expected record counts and never submit a real order. The focused suite passes
336 cases, including the original institutional risk acceptance.

Three separate in-memory guard removals reproduce behavioral failures: missing
pair-coverage enforcement, changed immutable history and missing SQL immutability.
Certified source files are not edited during those experiments. All existing test
files and acceptance assertions remain unchanged. An intermediate focused run
reported two failures because the new earlier check changed the legacy authority
error name; the original error contract was preserved rather than changing tests.

The Mac ran out of space before one expansion command executed. Only two verified
completed synthetic scratch directories from the preceding coverage certification
were removed; all 29 evidence reports/logs retained their hashes. No account or
unrelated working copy was removed. Exact full-run and CI results are in the PR.

A deliberate boundary test confirms that rolling back both the broker witnesses
and execution journal together is not detectable by this local check. Distributed
fencing, an external monotonic witness, authenticated authority and protection
against privileged rewriting of all local state remain separate requirements.
Position-linked reservation release is still unimplemented and was not retried
through this change. M01 and the 12-milestone closure plan therefore remain open.
