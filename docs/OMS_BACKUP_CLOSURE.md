# Bound-paper OMS backup and offline restore verification

Continuation of draft PR #126 from `d71370e7d5b429f890f34d33e9c10b0e1b2f016f`.
This closes omission of the separate OMS from explicitly reviewed recovery bundles.
It does not repair the four institutional risk findings, replace the daemon path,
perform a real backup/restore, stop a process, migrate an account or enable trading.

## Reproduced gap

A recovery bundle could capture a bound_v1 paper ledger without its separate OMS.
The first new test demonstrated that this incomplete capture was accepted. The old
specification also had no supported OMS selection or order-history verification.
Three further tests demonstrated fractional persisted OMS projection fields being
truncated by decoding and therefore escaping event-replay comparisons. Offline
capture now checks the raw integer fields before decoding/replay.

## Explicit selection and completeness

A bound account must supply the top-level `oms` path in its recovery specification,
in addition to the existing seven sources and any selected research_state entries:

```json
{"oms": "/data/REPLACE_WITH_CONFIGURED_OMS_DATABASE.sqlite"}
```

Use the actual existing PRAMANA_OMS_DB location, not the placeholder. The selected
path hash must match the immutable ledger configuration. No filesystem discovery,
environment search, guessed database or automatically created OMS is used.

The existing create/restore CLI is retained. A bound capture uses bundle schema 3
and records the original OMS path digest plus the captured verification result.
Legacy unbound bundles retain schema 2; older schema 1/2 bundles containing a bound
ledger but omitting its OMS now refuse restore before creating output.

## Verification without mutation

DurableOms now supports explicit read_only construction. It opens an existing
SQLite URI in mode=ro with query_only enabled, performs no schema initialization,
and uses read transactions for replay. The existing writable default is unchanged.
The captured OMS is integrity-checked, rejects unsupported table/version inventory,
and replays selected-tenant order events against their projections and fills.
Missing recovery-v2 audits, orphaned history and invalid raw quantities refuse.

Captured ordinary cash fills are compared by order identity, contract, units,
price and timestamp. Protective exits remain separate OMS-independent executions;
their ledger/evidence/outbox correspondence is checked before exclusion. This is
not full protective accounting, evidence authenticity or external reconciliation.

Pending orders and missing matches are preserved and produce a discrepancy report;
no guessed rejection, resubmission, recovery call or silently empty order book.
Restoring a valid recovered OMS retains its schema-v2 audit and the ledger's halt.
The stored original path pin is not rewritten: the report requires separate path
rebind/migration review. A restored file set does not authorize daemon startup.

## Evidence and boundaries

The added regression file contains 38 synthetic cases. It covers missing or wrong
OMS selection, legacy incomplete bundles, read-only access, event/projection damage,
retained recovery audits, WAL frames, concurrent source change, file aliases,
manifest/file tampering, interrupted-order preservation and no activation.
The surrounding recovery/runtime/OMS/coordinator set passes 179 tests. Exact full
normal-suite, failing risk-acceptance and published-head CI results belong to the
PR certification checkpoint; no external market-performance evidence is created.

Writers-stopped is a literal boolean operator declaration, not process attestation.
Existing pre/post file and nonempty-WAL fingerprints detect changes during capture;
this is not an atomic cross-host snapshot or a distributed deployment lease.
Each database is copied using SQLite backup, and restores never overwrite existing
paths. Copied bytes and event replay prove local consistency, not genuine fills,
licensing, authenticated reviewers or a profitable model.

The scheduled single-ledger backup service is unchanged and still does NOT capture
the complete OMS/accounting state automatically. This patch adds OMS to explicit
stopped-writer bundles only. Institutional execution programs, separate accounting,
AI registries and other state still need their own reviewed capture coverage.
No target-host or off-host restore drill was performed, and neither path migration
nor unattended recovery is enabled. Full institutional daemon assembly, the four
risk-acceptance defects, broader segment/settlement support and real data, operations,
security, human and forward-performance acceptance remain open.
