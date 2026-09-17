# Durable execution edge-policy binding

Continuation branch: `feat/post-126-paper-runtime`, from merged main/#126
`a20a0d833b83a094c372c54058c38bdc3c1d1afb`. The full institutional coordinator is
still not the daemon's default operating path. This change binds its edge policy;
it does not enable that runtime, connect a broker or allocate actual capital.

## Reproduced gap

The original coordinator re-read its mutable edge-gate configuration at each slice.
An approved parent could therefore be dispatched under a different policy. The
initial regression set demonstrated a looser policy after equity fell, reuse of a
decision under another policy, missing durable policy metadata and policy mutation
inside a provider callback. All four tests failed before the repair.

## Stored authority

New programs store a canonical `pramana.execution_risk_authority.v1` envelope with:
program/tenant identity; the original request hash; the approved parent-order hash;
ENTRY or COVERED_EXIT mode; every EdgePolicy parameter; and an explicit algorithm
version. The envelope's hash becomes the versioned runtime-context fingerprint.
The request itself remains caller-supplied and hash-checked, not fully reconstructed.

The execution journal adds a version marker and payload column atomically. Existing
records retain version 0 and missing policy, not invented provenance. New parent,
policy and slices commit together. SQL guards prohibit changing or clearing the
policy metadata; reads independently check canonical encoding and hash bindings.
Unknown versions, duplicate fields, malformed numerical declarations and corrupted
attribution refuse. This is local integrity, not a signature or reviewer identity.

## Dispatch, restart and completed fills

Preparation evaluates a defensive policy snapshot and checks it again before the
program is saved. Each child is evaluated using the stored policy, never a silently
substituted default. Configuration must still match before a dispatch claim and
immediately before OMS submission. A change detected before the claim leaves the
slice PENDING with a RECOVERY_REQUIRED result. A change during the claimed slice
fails that slice before a broker submission. Neither result authorizes a retry.

Restoring the exact original configuration can resume an unclaimed pending slice;
a changed policy requires a distinct reviewed decision, not overwriting the old
record. Any parameter change, including a stricter setting, holds new entry slices.
This conservative version matching is intentional; it does not automatically treat
a reconfiguration as approval. Recreating the same decision with another policy
fails its existing immutable decision/payload check.

New committed receipts include the saved authority digest. Both coordinator recovery
and offline institutional backup verification compare that digest against the
program binding. A changed current policy does not prevent accounting recovery for
a fill already committed under its original authority. No recovery re-evaluates
that historical fill as a new trade or creates another broker submission.

Covered sales retain their entry-policy exemption and normal Warden checks.
Independent protective exits remain outside policy/accounting availability. Legacy
records can still be inspected and their supported historical fills reconciled, but
new entry dispatch without a saved policy holds for reviewed migration. An opening
SELL cannot use an exit exemption to evade the saved-policy requirement.

## Evidence and limits

The new test file contains 44 synthetic cases. It covers every policy parameter,
configuration drift at preparation/dispatch/restart, concurrent different-policy
claims on the same decision, immutable database fields, malformed payloads, legacy
entry holds, receipt/backup matching, covered sales and independent exits. Abrupt
subprocess termination before and after commit confirms that policy and parent
state do not split. Changed policy does not prevent committed-fill accounting.
The focused coordinator/recovery/protection/acceptance run passes 186 tests.

Four isolated in-memory guard-removal experiments cause their corresponding tests
to fail. No certified source is edited by those experiments. Existing acceptance
assertions, test selection and runtime configuration remain unchanged. Exact final
full-suite and CI results are recorded with the published commit in the PR.

This binds EdgePolicy parameters and a declared algorithm schema, not every Warden
configuration, binary, provider session or deployment. Other request-carried limits
remain protected by the existing request digest. A checksum is not a signature:
a privileged actor able to rewrite both the database and its hashes is not defeated
by local append-only triggers. Reviewer authentication and signed release/model
bindings remain separate work.

Cross-program/shared-account risk reservations are still NOT implemented here.
There is no claim that the sum of concurrent programs fits a shared account loss
budget. Complete runtime request/liquidity-plan persistence, daemon/accounting wiring,
reviewed migration and rollback, automatic multi-store/off-host recovery, authentic
feeds and broker evidence, operational acceptance and forward after-cost performance
remain release gates. No real account, daemon, model or live-money mode was changed.
