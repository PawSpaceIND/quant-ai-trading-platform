# Remaining delivery plan after PR #126

Planning snapshot from PR #136 at `c42db823a0583a501faf0ff0345f514d00107e71`,
with main at `5bacb52337351c41fe782ac848edf1ae33647614`.
The 12 milestones below group the known remaining work; they are not 12 commits,
12 chat turns or a guarantee that testing cannot reveal further defects.
The original 33-capability register and its external evidence remain authoritative.
This plan does not replace them or mark any launch evidence complete.

Scope: milestones 1-10 target a private, integrated NSE cash-equity/ETF paper release.
Milestones 11-12 retain the requested broader multi-asset objective. They are not
quietly dropped to make the private pilot appear to finish the whole platform.
Live-money authorization and a public SaaS launch remain separate releases.

## Milestones and acceptance

| ID | Work package | Finished only when |
|---|---|---|
| M01 | Shared account risk and execution authority | Persisted policy and shared reservations cannot be overspent by concurrent programs; partial fills, uncertain submissions, cancellation and reconciled position closure preserve correct capacity; authoritative account/journal binding is enforced. |
| M02 | Complete decision/request/plan persistence | Exact inputs, sources, availability times, feature/model versions and full execution plan survive restart; replay reconstructs them without invented evidence or duplicate orders. |
| M03 | Full institutional daemon and accounting | Actual startup/tick loop runs AI proposal → allocation → portfolio risk → planner → OMS → paper broker → accounting, including recovery and independent exits; no separate test-only route substitutes for this. |
| M04 | AI candidate-to-model deployment chain | Authenticated review binds the exact dataset, trained artifact, evaluation and release; intended knowledge sources are qualified; shadow/rollback and drift controls work without self-granted risk authority. |
| M05 | Actual market data and broker qualification | Real session/profile, instrument master, timestamps, usable history/adjustments, costs and account/order reconciliation match the intended supported account and market; synthetic evidence is not substituted. |
| M06 | Authenticated operations and migration | Authorized review/confirm/recover/cancel/halt workflows have durable audits, exact identities, safe legacy migration and rollback; uncertain orders are never guessed absent. |
| M07 | Coordinated backup and off-host restore | Scheduled complete-state capture, consistent generation, secure transfer and a real restore/reconciliation drill preserve all necessary stores and existing halts. |
| M08 | Target-host deployment and operations | Adequate storage, exact release, working token renewal, health/clock checks, independent alert receipts, restart/rollback and sustained host operation are verified. |
| M09 | Integrated human and security acceptance | Desktop/mobile journeys cover login, watchlist, AI, entry/rejection, slices, exits, accounting, reports and recovery; negative authorization/tenant tests and independent security review are complete. |
| M10 | Demonstrated strategy/calibration evidence | Exact candidate passes declared holdout/walk-forward/stress/forward-paper gates after costs; qualified samples, drawdown and calibration meet the frozen policy. Profitability is measured, never guaranteed. |
| M11 | MCX futures completion | Pilot admission, exact margin/fee sources, authentic contract-note reconciliation, short/expiry/roll/delivery and daily-settlement handling are implemented and qualified for the chosen contracts. |
| M12 | Remaining segments and currencies | Options fee/spread-margin/assignment fidelity and other requested venue/instrument segments, contract routing, currency translation and settlement each have explicit implementation and genuine source/broker evidence. |

## Dependency and effort boundaries

M01 → M02 → M03 is the immediate engineering critical path. M04 can progress beside
M02/M03; M05 real-source qualification should start as soon as source/account access
is available. M06/M07 must be ready before M08 recovery acceptance. M09 certifies
the integrated release, not isolated modules. M10 requires qualified observations;
it cannot be completed by rerunning synthetic tests or increasing their count.
M11/M12 depend on contract/data/accounting/risk foundations and are separate segment
programmes, not a single small patch. Existing implemented modules should be reused.

Sources: `docs/SUPER_PLATFORM_CLOSURE.md`, `governance/super_platform.py`,
`docs/PILOT_CLOSURE.md`, `docs/ENGINEERING_CHECKPOINT_126.md` and PR #136.
This grouping is a delivery plan derived from those scopes, not a new scored audit.
No dated finish estimate is supported while external access, host acceptance and
forward effectiveness remain unverified. The recorded pilot contract mentions
100 trades / 30 days thresholds; reaching them alone does not establish an edge.

## Current continuation — M01 partially implemented

The shared-reservation change now provides opt-in same-journal atomic admission,
current-equity capacity rechecks and never-claimed cancellation. Reservations remain
charged after partial fills, uncertain dispatch, completed buys and position exits.
That conservative last rule prevents invented capacity but is not the final
position-linked release lifecycle. The subsequent local broker-binding patch pins
the selected journal identity/path in the paper ledger and rejects unlinked buys.
Used-risk release, full authenticated-account authority and rollback/cross-host
fencing remain part of M01. See `SHARED_RISK_BROKER_BINDING.md` for the local binding
checks and precise limits. No milestone is marked complete by component tests.

No real account policy was set. PR #136's pinned EdgePolicy is a prerequisite,
not a replacement for shared account capacity. The private daemon remains unchanged.

### Committed-entry journal coverage continuation

The selected broker/journal pairing now also verifies that all recorded entry fills
remain represented by the matching retained parent and slice claims. A same-identity
journal rollback cannot hide a BUY still present in the broker ledger. See
`SHARED_RISK_COMMITTED_COVERAGE.md`. This is not detection of lost unexecuted history
or simultaneous rollback of all stores. Position-linked capacity release and the
remaining account/fencing boundaries stay open; M01 and the milestone count are unchanged.

### Pending-reservation rollback continuation

New v2 shared-account pins now retain broker-owned admission witnesses even before
any purchase fills. Restoring only an older execution journal cannot silently lose
those reserved programmes: pairing, admission, final BUY and offline checks refuse.
Journal failure after the broker witness commits also leaves an explicit hold.
Old v1 pins require reviewed migration for runtime use; no actual migration occurred.
See `SHARED_RISK_ADMISSION_WITNESS.md`. This closes the named missing-never-executed
reservation case relative to the retained broker ledger, not rollback of both stores,
cross-host fencing or authenticated external authority. Used-position release remains
unimplemented, so M01 stays partial and the 12 milestone count is unchanged.

## M02 component continuation — complete supplied request/plan storage

The `feat/persisted-institutional-context` branch, based on refreshed #137 at
`edad0e73a44f32bb332f53a08a55aa5dbb2aa071`, adds typed storage of every current
InstitutionalTradeRequest and ExecutionPlan field. Parent, slices and snapshot
commit together. Explicit reconstruction checks approved hashes, full liquidity
inputs and programme identity without creating an order or fetching new data.
Offline restore reports retained-context coverage separately from activation.

This proceeds on an independent persistence component while M01's position-linked
release remains open; it does not waive or retry that release work. Full M02
acceptance still needs actual source/feature/model lineage and release evidence
from the integrated producer path. M03 daemon wiring, M01 completion and the other
milestones remain open. No current account or running daemon is migrated or switched.
See `PERSISTED_EXECUTION_CONTEXT.md` and the scoped PR's exact test evidence.

### M02 integration follow-up — selected accounting namespace

Review of the stored-context restart path reproduced a genuine attribution defect:
a request/fill for one tenant could be completed using another tenant's accounting
adapter. Existing #140 now gains explicit process-local accounting scope checks at
preparation, binding, dispatch and recovery, plus transactional trade/cost posting
and stable protective-accounting selection. Mid-posting scope drift rolls back
journal records while retaining the committed fill for idempotent recovery.
See `INSTITUTIONAL_ACCOUNTING_SCOPE.md`. This closes the named misattribution paths,
not authenticated account ownership or a persistent signed accounting-file bind.
M01 release, full M02 producer/source lineage, M03 runtime assembly and the remaining
milestones stay open; no live setting or real account is changed by these tests.

### Persisted accounting-store selection prerequisite

New programmes retain the selected local accounting-store identity, tenant, base
currency and path. A reconstructed coordinator verifies that retained selection;
copy/restore verification checks identity even when paths intentionally differ.
This extends the earlier process-local accounting guard across restart for explicitly
bound programmes. Older unbound rows remain reported unverified. No position-risk
capacity is released, no daemon is activated and no full milestone is marked complete.
See `DURABLE_ACCOUNTING_SELECTION.md` for the local identity and rollback limits.

### M03 immediate operating-path bridge (partial)

An explicitly selected `InstitutionalRuntimeInputs` configuration now connects the
existing synchronous/asynchronous swarm route and actual runner factory to the
institutional coordinator, programme journal, shared-risk admission and accounting.
It requires bound NSE cash, immediate execution and supplied institutional inputs;
missing inputs cannot fall back to direct submission. Default deployment selection
is unchanged. Trace provenance and final daemon operating checks are retained.
Automatic scheduled execution/recovery, source qualification, M01 capacity release
and target-host acceptance remain open. See `INSTITUTIONAL_SWARM_BRIDGE.md`; no whole
milestone is marked complete by the synthetic bridge tests.

### M03 immediate-bridge recovery component

The selected immediate institutional swarm service now has an explicit historical
reconciliation API. It reconstructs saved bridge inputs, verifies original decision
trace/receipt identity and completed trade/fee postings, and uses existing OMS and
accounting recovery without new orders or current market providers. Missing receipts
retain uncertainty; independent exits and reserved risk are unchanged. This is not
authenticated operator review, unattended recovery or scheduled programme service.
M03 and M01 remain partial. See `INSTITUTIONAL_BRIDGE_RECOVERY.md` and PR #146.

### M03 actual immediate daemon-cycle acceptance

Actual build_ghost_runner/run_once testing exposed and repaired a final-preflight
self-block: the daemon had mistaken its own freshly submitted institutional child
for unrelated unresolved OMS work. The final phase now validates that exact claimed
child while initial admission/restart retain the full unresolved-order fence.
Synthetic cycles exercise required book-risk gates, accounting failure/recovery,
independent protection and off-hours behavior without SDK or model network calls.
The immediate component has real daemon-cycle coverage; scheduled lifecycle,
authenticated rollout, qualified sources and the remaining M03 gates stay open.
See `INSTITUTIONAL_DAEMON_CYCLE.md` and the exact PR #146 certification.
