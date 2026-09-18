# Operator recovery audit in stopped-writer bundles

Base: PR #148 at `81911df5966a12ecaeb2064e95824ef83107b54c`.
This closes explicitly selected audit retention and offline verification alongside
the existing paper ledger, OMS, accounting and execution-programme backup. It does
not deploy automatic backups, recreate credentials, resume a daemon or replay recovery.

## Selection and compatibility

The existing recovery-bundle create command accepts an optional top-level
`operator_audit` containing the absolute or caller-resolved path to the existing
operator audit database. The selected OMS and both institutional stores are required.
The audit must be an existing unaliased regular file and distinct from all other
sources. No runtime constructor or filesystem search supplies a missing file.

An audit-bearing bundle uses schema 6 and includes the fixed `operator-audit` entry.
The manifest records its checksum, size, original source-selection digest and verified
history report. Existing schema 1-5 handling and all OMS/accounting/programme/research
and AI checks remain active. Schema 6 also composes with an explicitly selected AI
inventory. An unselected audit remains `not_selected`; omission is not complete audit
coverage. No durable account-level audit-selection pin has been added.

## What is verified

The existing operator audit replay is factored into a read-only function, shared by
its runtime reader and the offline verifier. Its schema, sequence, canonical JSON,
hash chain, tenant, bounded record sizes and request/outcome transitions remain
required. The capture also checks the declared source-path selection independently
against the selected OMS, programme, accounting and paper-ledger paths in the same
order used by the operator service. Original audit rows are not rewritten on restore.

Every audited programme and exact saved-context checksum must exist in the selected
programme store. Returned results must match the original provider-revision digest,
retained broker receipt IDs, actually executed sequences and immutable decision trace.
A claimed completed outcome must have its corresponding complete source programme and
receipts. All four underlying institutional stores are independently reverified by
the existing offline verifier before this audit-to-source comparison; a valid audit
checksum is not sufficient by itself.

The report distinguishes returned requests, failed requests and requests without an
outcome. A REQUESTED record left after an interrupted outcome write remains unknown,
even when its bookkeeping already committed; the overall restore reports discrepancy
until operator review. A historical FAILED record is not converted into a rejection
or a new retry. Old returned outcomes do not become current-account health assertions.
Future-dated audit events beyond the recorded capture time refuse.

## Restore behavior and non-authority

Only SQLite backup/copy and read-only checks run. Committed WAL frames are included.
The new component never constructs a runtime, issues credentials, invokes recovery,
places an order, clears a halt, rebinds a path or releases reserved risk. Existing
halt files remain present. The report retains activationAuthorized=false,
recoveryReplayed=false and credentialStoreCaptured=false.

Canonical report comparison preserves booleans versus numeric values. Malformed
inventories, unavailable audit files, older schema labels carrying audit data, changed
source selection, mismatched programmes/receipts and changed verification reports
refuse. A failed restore removes only its newly created partial output; an existing
output directory is never overwritten. Pre/post fingerprints still detect source
changes during capture. Writers-stopped remains an operator declaration, not a process
stop command or a distributed transactional snapshot.

## Evidence and remaining operational requirements

45 new synthetic tests cover successful capture/restore, interrupted and failed
audit outcomes, empty/unselected audit state, path/tenant/identity problems, rewritten
and rehashed evidence, canonical verification, WAL contents, reader limits, source
drift, AI composition, preserved source files and forbidden recovery/credential calls.
The initial two failing tests specified previously unsupported bundle coverage; they
are capability specifications, not two pre-existing operational incidents.

Final focused/full-suite and exact checked-commit evidence is recorded in the PR.
No pre-existing test or acceptance assertion is changed, and no scan/CI exception or
risk threshold is added. The shared runtime audit reader is refactored, not given a
new write or authorization path.

This remains selected offline retention. Automatic coordinated scheduling, audit
selection pinned independently of caller configuration, off-host authenticated storage,
credential persistence/rotation, reviewed runtime migration, independent security and
human acceptance remain open. Privileged rewriting or coordinated rollback of all
stores is not prevented by local checksums. A copied audit is not evidence of a
verified human or external brokerage account. No whole delivery milestone or trading
profitability is claimed.
