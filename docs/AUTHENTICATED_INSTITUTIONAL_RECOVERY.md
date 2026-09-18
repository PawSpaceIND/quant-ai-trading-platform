# Authenticated institutional bookkeeping recovery

Base: PR #146 at `cb1f1590d4ad70fd2ef98ad8d2999f1f1443850b`.
This is a separate opt-in M06 component over the existing immediate bridge's
historical `reconcile_program` operation. It does not add order submission,
automatic retries, halt clearing, position-risk release or a production rollout.

## Server selection and access

The application selects a mapping from tenant to `InstitutionalRecoveryOperations`.
Each entry binds one existing runtime and a separate durable audit database. The
request cannot choose a tenant, storage path, runtime, model or provider. The default
ApiState mapping is empty, so no actual account is implicitly attached to the API.

The existing API-key registry now accepts explicit permissions when issuing a key:
`institutional.recovery.read` and `institutional.recovery.apply`. Existing keys have
neither permission. There is no HTTP endpoint to issue or upgrade credentials.
The shared authentication and rate limiter still apply. A previously authenticated
key is checked again after obtaining the runtime lock; revocation while queued
prevents the operation. Revocation does not undo an already authorized operation.

These are tenant-scoped possession credentials, not an independently verified human
identity, multifactor approval or a two-reviewer authorization system. The registry
and revocation state are currently in memory. Production persistence, secret
provisioning, TLS, external identity, expiry and operational rotation are separate
requirements. No existing credential was issued, rotated or revoked in deployment.

## Review and confirmation endpoints

`GET /v1/institutional/programs/{program_id}/recovery` requires read permission.
It returns the stored context digest, programme/slice states and a bounded scope
description, without reconciliation. The provider revision is represented by its
hash, not arbitrary provider text. The preview is not a market-data certificate.

`POST /v1/institutional/programs/{program_id}/recovery` requires apply permission
and exactly three string fields: `request_id`, `expected_context_sha256` and
`confirmation`. The confirmation must equal
`reconcile_recorded_paper_bookkeeping_only`. Unknown fields, query overrides,
malformed identifiers and a different saved context refuse before source changes.
Responses and validation failures use no-store headers; validation errors do not
echo submitted values. Internal exceptions never expose source paths or SQL.

`GET /v1/institutional/recovery-requests/{request_id}` requires read permission and
returns the selected tenant's recorded request status. There is no unscoped listing
or user-selectable tenant. The record identifies the authenticated key ID, not its
raw key or key hash. Returned broker IDs refer to historical fills, not new events.

## Write-ahead audit and interruption

An explicit REQUESTED record commits with SQLite synchronous FULL before invoking
historical reconciliation. RETURNED records the bounded returned report; it does
not imply the report says COMPLETE. FAILED means the invocation raised and requires
review, not that no bookkeeping committed. Missing receipts remain uncertain.

A retry of the same returned request replays the recorded response without calling
reconciliation again. Reusing the ID for another key, programme or context refuses.
A REQUESTED record with no outcome, including a crash after source writes but before
the final audit commit, refuses automatic replay. A separately reviewed invocation
must use a new request ID. Prior events remain unchanged; source idempotency still
prevents repeating the original trade. The receipt is never interpreted as an
execution permission, and every result retains `execution_authorized=false`.

Audit events are append-only under the normal SQLite triggers and are hash-linked
with validated identities and status transitions. A maximum of 10,000 events and
64 KiB per event bounds inspection; exhausted capacity refuses before bookkeeping.
This is not authenticated external storage or protection from privileged rewriting,
coordinated rollback or deletion of every store. A new file after an offline loss
is not proof that an older audit never existed. Reviewed backup/migration is needed.

## Preserved execution boundaries

The HTTP wrapper invokes only the existing historical reconciliation operation.
No market/calibration/factor/liquidity provider, new order, automatic cancellation,
position reopening, halt clearing or reserved-capacity release is added. Existing
recorded protective exits for the same tenant may be reconciled by the underlying
operation; the preview states that scope. Missing receipts are not retried.

The service uses the runtime's in-process lock for admission and repeated calls,
and the journal uses a SQLite transaction for each audit append. It does not hold
a cross-database transaction across reconciliation and is not a distributed lock.
Concurrent processes depend on existing source idempotency and may require review
on contention. A returned historical response is not current account health.

This new audit store is not automatically included in the existing recovery bundles
or deployed scheduled backups. Coordinated inclusion, external retention, migrations
and real off-host restoration remain explicit M07 acceptance requirements.

## Regression evidence and scope of authentication

52 new synthetic tests exercise the real FastAPI handlers, scoped/revoked API keys,
tenant selection, exact-context confirmation, retained audit and actual bridge
reconciliation. They cover read-only recovery permission, forbidden storage/action
fields, redacted validation failures, missing receipts, conflicting request IDs,
source/accounting failure, audit failures, corrupt history, size limits, concurrent
HTTP retries, reopen, independent protection and three process-death boundaries.

Two initial specifications failed because scoped recovery was not implemented.
Two later development cases demonstrated queued-key revocation and validation-value
reflection gaps. Both were fixed and retained without changing earlier assertions.
Five isolated in-memory guard-removal tests detect permission, context, write-ahead
commit, queued revocation and audit-chain regressions. No existing test was weakened.

Final local and GitHub results, precise commit identities and residual risks are
recorded in the scoped PR. No credentials, genuine market data or account state are
included in test fixtures or the audit examples. Dynamic test keys are generated
only for disposable in-process test servers and are never printed in final evidence.

The audit begins with authorized, validated recovery requests. It is not a durable
security-event sink for every unauthenticated request. The added scopes apply to
these recovery endpoints; this patch does not convert all older API routes into a
new role model. A gateway, identity service and independent authorization review
remain part of deployment acceptance. No whole milestone is marked complete.
