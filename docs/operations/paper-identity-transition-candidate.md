# Local forward-only paper identity transition candidate

This candidate is not a deployed migration, a CLI, a selector activation or an approval
of source identity. Tests use synthetic records only. A reviewed authoritative NSE cash
catalog and account binding must be supplied; hashes bind declarations and do not
independently authenticate them. The catalog cap remains 50 and risk/capital/quota gates
are unchanged. No external broker credential is provisioned.

`inspect_oms` and `inspect_transition` use existing private regular files, SQLite mode=ro,
query_only and read transactions without store constructors. Missing files are not created.
The planner rejects physical zero-quantity rows, ambiguous/unmapped holding identities,
unsupported modes/contracts, conflicting pins, corrupt accounting and any raw history in
any OMS table globally. Unknown OMS tables refuse. Private plan material contains the
unchanged historical rows; it must not be published. The plan deliberately states
apply_supported=false because there is no operator-facing supported apply interface yet.

Explicit `provision_empty_oms` inserts a separate immutable singleton UUID, only after
matching a reviewed empty-schema fingerprint. This is an internal database identity,
not a credential. Repeated provision preserves identity; empty same-path replacement
has missing/different identity. Copies retaining UUID still require history-integrity
qualification; UUID/path alone is not authentication.

The local library apply primitive acquires write locks on both existing stores, revalidates
the whole plan, then atomically inserts the ledger certificate/configuration pin and binds
current holding identities. It performs no OMS writes or historical ledger updates. OMS
provisioning occurs separately beforehand; a provisioned-only crash does not migrate the
ledger or grant admission. Applying the exact same reviewed plan is idempotent; differing
bindings refuse. No inverse or certificate deletion interface exists: recovery is forward-only.

The same TransitionReplay boundary supplies effective holding identity to reconciliation,
prior-position receipt validation and protective accounting. Original fills remain unchanged.
The complete legacy cohort and its costs/evidence/outbox are verified from the certificate.
Only after its final relevant legacy fill does replay validate quantity/average and interpret
current holding identity as the reviewed bound snapshot. Old receipts remain evaluated from
their original prefix. New/low-ID or null-identity fills are never added to the cohort.

Entry reconciliation recognizes only that exact verified cohort without inventing historical
OMS events. Subsequent strategy fills retain strict OMS matching. Protective exceptions now
require the existing validated SELL receipt/evidence/outbox hash. Missing/replaced OMS identity,
certificate tampering or altered legacy records hold entries. Independently valid protective
SELL execution remains outside the entry certificate gate; receipt/accounting qualification
can still fail separately and must not be mislabeled as reconciled.

No host execution is authorized by this module. Before any approved operational migration:
quiesce all writers, agree a protection interruption window, retain verified WAL-safe backups
and private config/image inventories, and rehearse isolated restore. Binding current holdings
can make old unbound SELL orders incompatible. Never restore an older ledger over newer fills
or erase the transition certificate. Any post-cutover activity in either database requires
forward recovery; this candidate offers no automatic image/data rollback.

Selector prerequisites remain separate: paper pilot bound-v1 wiring, fresh observed mapped
Zerodha prices and complete qualified cache coverage. Migration does not populate history,
change subscriptions, activate the selector or establish a trading edge.

Acceptance includes original refusal behavior, all-or-nothing mutation and revalidation,
unchanged legacy records, first BUY/partial-full SELL/protective SELL receipts, old receipts,
restart/reconciliation, shared protective accounting replay, raw orphan history, missing and
same-path replaced OMS, low-ID/backdated later fills, and entry-certificate failure with
independent protective execution. Further independent review/host rehearsal and hosted CI
are required before an operator-facing migration workflow is published or used.

## Successor review repairs

Holding admission compares symbol, market and asset class against the selected catalog;
all replay consumers use that complete holding key, including flat other-class history.
OMS qualification requires the fingerprint of the trusted fresh DurableOms v1 DDL and
metadata, including keys/constraints and immutable history triggers. Arbitrary schema
fingerprints are not supported. Provisioned UUID table constraints/triggers are also
validated exactly. Previously recovered or differently formatted/migrated OMS schemas
refuse and require separate review; this candidate does not adopt them automatically.

The private plan binds complete original ledger DDL. Unknown or altered ledger triggers
refuse; the initial allowlist is the four current broker-generated immutable outbox/shared
witness triggers. Additional legitimate host triggers therefore need review, not bypass.
Replay also rejects unreviewed post-transition DDL. During apply, all pre-existing tables
are compared globally before commit, permitting only the reviewed tenant's current holding
identity changes. Cash, quantities, protection, costs, original claims, history and other
tenants remain exact. Transition-aware reconciliation and complete protection are checked
inside the transaction. A failed check rolls back certificate, pin and holding changes.

Tests separately bypass trigger admission on synthetic databases to establish that the
post-write guard rolls back cash, quantity, historical fill and inserted-cost mutation.
These caught-exception tests are not hard-process-crash or host power-loss qualification.

## Supported startup-schema preflight

A transition must begin from a fully initialized supported legacy runtime, not just a
bare broker database. The read-only planner requires the existing pilot_scope schema
and exact tenant/catalog scope, plus the trusted current runtime DDL for journal,
valuation, specialist feedback, feed/runtime/manifest and risk-state tables/index/triggers.
Missing or differently initialized runtime schemas refuse BEFORE apply; the planner does
not create or migrate them. The trigger allowlist includes the two audited immutable
specialist-feedback triggers in addition to the four broker triggers.

Synthetic fixtures now initialize via the actual legacy daemon builder before inspection.
The actual bound daemon builder is then constructed and restarted after transition, with
its real configure_pilot path, and reconciliation remains matched on both starts. No
streams, orders, provider requests or runtime flags are activated by this test. An operator
must separately qualify existing host schema; this preflight is not permission to initialize
or restart the host. Differing supported historical schema forms require explicit review.
