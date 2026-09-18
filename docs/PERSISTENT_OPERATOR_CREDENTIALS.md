# Explicit durable operator possession credentials

Base: PR #150 at `a2061b0750f0e969635bb968d1a81cf0b6c5726a`.
This is a scoped M06 prerequisite: persistent credential grants, explicit expiry,
revocation and rotation for the existing API. It does not select or deploy an account,
add an identity provider, enable trading or complete the full authentication milestone.

## Selection and compatibility

The server may explicitly supply `PersistentApiKeyRegistry` as `ApiState.keys`.
The existing ApiKeyRegistry, application defaults, recovery route permissions,
rate limiter and queued-request revalidation remain unchanged. No environment flag,
HTTP credential-issuance route or automatic migration/import is added.

Creation requires `create=True` and an unused local path. Reopening is the default
and requires the existing database. Missing, unrelated, aliased, publicly accessible
or malformed stores refuse; no replacement store or permission grant is inferred.
New files are owner-only mode 0600. The registry holds no raw keys in its database:
issuance returns a freshly generated possession secret and the existing ApiCredential
representation; the durable record stores its digest and immutable declared grant.
Protect this database and its backups as security-sensitive authorization material.

`issue(tenant_id, scopes=..., expires_at=...)` requires explicit aware expiry.
There are no recovery permissions by default. Only the existing recovery read/apply
scope names are accepted. At most 10,000 retained grants and a 365-day maximum
lifetime bound this component; the maximum is a validation ceiling, not recommended
operational rotation frequency. No key has unlimited lifetime.

`authenticate` and `is_active` re-read durable state rather than trusting a cache.
Revocation by another connection is observed on the next authorization check and
survives process restart. Missing/corrupt/unavailable storage denies authentication;
trusted server issuance/rotation operations raise bounded errors instead of exposing
paths, SQL or credential values. Authentication failures remain non-authorized.

## Expiry, rotation and transaction boundaries

Expiry is exclusive: at the expiry instant a credential is no longer active.
The store retains the greatest observed UTC time and refuses clock regression;
restarting with an earlier time cannot make an already expired credential valid
while that recorded clock state is retained. This is not authenticated external time
or protection against coordinated rollback of the whole database. A forward clock
mistake may deny service until time is corrected; no automatic allowance is applied.

`revoke(key_id)` records a final revocation. Normal database triggers prevent grant
rewrites, deletion, reversing revocation and moving the recorded clock backward.
`rotate(key_id, expires_at=...)` preserves the exact tenant and permissions and
commits the successor grant and old-key revocation in one SQLite transaction. An
invalid expiry, capacity limit, collision or write failure cannot leave a half-rotation.
Two competing rotations cannot both consume the same still-active source key.

A crash before the rotation transaction commits retains the old grant. A crash after
commit leaves the old key revoked even if the caller never received the successor
secret. Secrets cannot be recovered from the store; trusted re-provisioning needs a
new explicit action. An interrupted outcome is not a reason to reactivate an old key.
SQLite synchronous FULL is selected; independent host/storage durability acceptance
is still required. This is a local file, not a multi-host credential service.

## Integration evidence

61 new synthetic cases cover restart, permission/tenant boundaries, exact expiry,
clock rollback, cross-connection revocation, queued revocation/expiry/rotation,
unsafe or missing stores, malformed/rehashed grants, immutable records, failed and
competing rotation, capacity, source-secret absence and three abrupt-process exits.
The actual recovery HTTP handlers run with a reopened registry: ordinary/read-only
keys cannot apply recovery; an authorized key can reconcile the original paper fill
with the same audit actor and no new order. Queued credentials are rechecked by the
existing handler. No production execution or recovery code is changed.

The three initial specifications failed because this component did not exist.
One intermediate test-edit run lacked an import removed earlier by unused-import
lint cleanup; it was corrected only in the new test file, not relabelled as a passing
baseline. Full frozen-tree and exact CI evidence is recorded in the PR. Tests issue
only ephemeral synthetic secrets to in-process servers and never print real keys.

## Remaining boundaries

This authenticates possession of a selected tenant key, not a verified human, MFA,
dual control, signed broker ownership or independently protected authorization.
Privileged users able to rewrite the whole database can replace local digests and
policy. Storage identity is checked during a process; replacement/rollback across
a stopped process requires external retained evidence and reviewed operations.

No grant/revocation cache survives as an alternative authorization source. Each
authorization uses a local SQLite writer transaction to record the clock; capacity,
contention and performance need target-host acceptance. The component supports the
existing local macOS/Linux model and does not claim network-filesystem correctness.
Raw secrets must be provisioned through an appropriate secure channel outside this
API. TLS/gateway, credential-manager integration, durable identity audit/rotation
operations, operator UI and independent security acceptance remain open. The recovery
scopes do not change permission handling on older non-recovery API endpoints.

Credential material is deliberately not added to recovery bundles. Secret-store
retention, independently protected audit/identity pins and tested credential recovery
need separate reviewed policy. M01 position-risk release, M03 scheduled execution
and the remaining platform/real-source/forward-performance gates are unchanged.
No real account, credential, running daemon, deployment, model or live-money setting
is changed by this implementation or its tests.
