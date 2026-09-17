# Durable local accounting selection

Continuation of draft #140 from `59d6cc8e0cc096fba6b93df11767a1ae3bd44f2e`.
This binds the selected accounting store across coordinator reconstruction. It does
not assemble the institutional daemon, release risk capacity, authenticate an
external account or change any running service.

## Reproduced boundary

Two tests failed on the published base. After one preparation, a newly constructed
coordinator could select a different accounting database carrying the same tenant
label, reload the stored programme, or approve another programme for that tenant.
The prior object-identity check correctly detects an existing coordinator's adapter
change, but its binding is not retained through construction of a new coordinator.
Both reproductions use disposable paper stores and do not execute live orders.

## Retained identity

A new preparation provisions a random local store identity in the existing
accounting metadata table. It records that identity, the selected tenant, base
currency and actual SQLite file path in the execution programme. The programme's
binding and accounting store identity are immutable under the provided SQL guards.
The original namespace/object/connection checks remain active.

All explicitly bound programmes for a tenant must select the same accounting store.
The check is repeated inside the programme writer transaction so competing initial
preparations cannot split the tenant across two independently selected stores.
The initial operator's selection remains the authority; this does not independently
prove it belongs to the intended broker account.

## Runtime and offline behaviour

Reload and subsequent execution/recovery compare the retained programme selection
with the current journal metadata. Missing identity, a different store at the same
path, or an identical copy at a different runtime path refuses before new submission.
Inspection does not repair a missing identity. Incorrect store selection cannot
silently become acceptable merely by constructing another coordinator.

Offline backup verification ignores the copied path but still requires the original
store identity, tenant and base currency. It reports verifiedAccountingBindings and
legacyMissingAccountingBindings separately. Existing receipt, OMS, accounting and
shared-risk verification remains active. Copying successfully is not permission to
resume; reviewed path migration is still required. Global activation remains false.

The identity metadata commits before the programme transaction. Interruption can
therefore leave an unused identity with no programme, reservation or trade. This is
not an atomic transaction spanning two databases. A committed programme contains its
binding with the other programme fields; restart reads that exact selection.

Older programmes and low-level callers with no binding retain a null value and are
reported unverified, not assigned invented historical selection. This component
does not retrospectively certify those programmes or add a general migration UI.
Privileged removal of all bindings and identity metadata is outside the protection.

Independent protective exits are unchanged. An invalid accounting selection holds
this coordinator's posting/recovery path, not the independent engine's ability to
close exposure. No reservation is freed by this change.

## Test and operational evidence

The new regression file covers reconstruction of a coordinator, reopening the same
store, substitution by a same-label store, copied paths, changed/missing identities,
SQL immutability, malformed binding payloads, good/bad offline backups, explicit
legacy reporting, concurrent selections and independent protection. Two subprocess
termination boundaries and a parent-transaction failure check preserve the distinction
between unused local identity metadata and committed programme authority.

The baseline two tests reproduced the missing restart check before implementation.
Subsequent local suites encountered SQLite disk-I/O errors as the Mac approached
full storage; these runs are retained as failed/partial evidence, never passes.
One new test initially imported a nonexistent helper; it was corrected to invoke
the existing ProtectiveExitEngine directly. Complete clean-runner results are
recorded in the PR, not inferred from those local runs.

Three exact completed scratch roots from the preceding accounting-scope task were
removed after verifying its finished run; all 34 retained report files kept their
checksums. No account data or unrelated worktree was removed. With free space again
low, further local full-suite reruns were stopped rather than deleting more user data.

## Remaining limits

These are local, unsigned storage identities, not authenticated broker ownership,
a distributed lock or a monotonic off-host authority. Identical copies restored to
the same path, coordinated rollback of all stores, privileged identity rewriting
and missing older programme bindings remain outside this guarantee. An in-memory
journal has no durable host identity. The operator must still select the right
initial stores. M01 release, complete M02 evidence lineage and M03 operating-loop
integration remain open; this closes a prerequisite restart-selection defect.

A programme from another tenant is rejected before changing the coordinator's
cached durable selection. The negative rebind test also executes the original
legitimate programme afterwards, proving refusal does not poison its selected scope.
