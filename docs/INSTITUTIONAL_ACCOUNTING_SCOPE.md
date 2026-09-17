# Institutional coordinator accounting scope

Continuation of draft #140 from `bcf43b986853d39e7d3fd57bc7b8a045fa0cb272`.
This repairs namespace/store selection across preparation, explicit context reload,
execution and historical accounting. It does not release risk capacity, configure
a real account, enable a daemon or create an authenticated operator endpoint.

## Reproduced defect

Three regressions failed on the unchanged published base: preparation accepted an
accounting adapter for another tenant; saved context rebound to that adapter; and
a tenant change after the paper broker commit still returned COMPLETE. A separate
synthetic reproduction observed one fill for `tenant`, one TRADE posting for `other`
and a COMPLETE programme. This was an actual accounting misattribution, not merely
a missing status label. No live broker or actual account was used.

## Selected scope

A coordinator snapshots the explicitly supplied accounting tenant, journal object,
SQLite connection object and base currency at construction. Tenant identity must
be a nonempty stripped string. The supplied request and current accounting adapter
must match that selection before preparation and again after preparation callbacks,
before binding saved context, at execution/recovery checks, and immediately before
broker submission after OMS callbacks. A mismatch refuses with the bounded error
`institutional_accounting_scope_mismatch`, without disclosing a filesystem path.

Journal/connection checks detect an existing coordinator redirected to a different
store even when the tenant label is reused. A newly constructed coordinator still
needs correct operator-supplied storage selection. This is a process-local binding,
not a durable, signed identity for an external broker account or accounting file.

## Posting and recovery

The paper broker/OMS still commit before the separate accounting store. A change
after the fill leaves FILLED_UNACCOUNTED rather than misposting and claiming completion.
Trade and fee records for that fill now share a journal transaction. The scope is
checked before posting and before the transaction completes, so an injected tenant
change within a posting callback rolls the journal writes back together. It never
rolls back or repeats the already committed paper fill.

Correctly scoped explicit reload/reconciliation uses the existing receipt, OMS,
contract, quantity, price and cost-basis checks. A repeated reconciliation is
idempotent. Scope drift before claim leaves pending slices unchanged; drift during
a claimed slice retains the dispatch/recovery fence and never guesses that an
uncertain order is absent. No new automatic retry or fence-clearing path is added.

Protective accounting checks the selected scope and uses a stable wrapper for that
journal and tenant. The independent protective-exit engine is unchanged: it can
close exposure even when this coordinator's accounting selection is invalid.
Correctly scoped ordinary covered sells retain their existing entry-only exemptions.

## Verification

35 new cases cover wrong or malformed namespaces, same-label store/connection
switches, changes in preparation/dispatch/posting callbacks, read-only reload
refusal, exact restart after a partial TWAP, recovery after a committed fill and
continued independent exits. Two subprocess-death cases cover an uncommitted
foreign-tenant posting and interruption after the correct accounting commit.

The focused integration suite passes 393 tests including both original acceptance
files. Four isolated in-memory guard removals reproduce failures in tenant scope,
final submission checking, selected journal identity and posting atomicity. The
published test assertions are unchanged. An intermediate integration failure was
an earlier context-not-found error being masked by the new scope check; the old
error ordering was preserved instead of altering its test. Final full-suite and
exact-commit CI results are recorded on #140.

The Mac had little free space. Four exact completed synthetic scratch directories
from the prior persistence certification were removed only after confirming the
runs had finished; all 61 retained evidence/report files kept their checksums.
No real account database, credential or unrelated worktree was removed.

## Remaining limits

This validates local selected identifiers and store objects. It does not prove
operator permissions, broker ownership, a persistent accounting-file identity or
security against arbitrary in-process code that rewrites all private fields or
writes directly through another database connection. Deployment binding and
cross-host authorization/fencing remain separate requirements. The existing
journal/receipt reconciliation requirements are not waived.

M01 position-linked risk-capacity release remains unimplemented. The earlier
blocked release operation is not retried through this independent accounting fix.
Complete producer/source/model lineage and the full institutional daemon assembly
also remain open. No milestone, live-money readiness or profitable edge is claimed.
