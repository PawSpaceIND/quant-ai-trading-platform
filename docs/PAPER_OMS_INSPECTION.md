# Read-only paper order inspection in the private workspace

Continues draft PR #126 from `bbd730c369399ab7f3aa0d43839e8e06332a85c6`.
This is an operator visibility addition, not a trading-risk repair, an order
submission path or an authenticated approval/recovery console.

## Actual path

The Activity view mounts `PaperOmsPanel`, which requests `GET /api/paper-oms`.
The handler checks the existing signed workspace session itself, in addition to
the shared proxy. Missing configuration/session refuses before any store access.
The selected tenant and database paths come from server settings, never request
parameters. Query parameters that could suggest tenant/path/action overrides
refuse. There is no POST, retry, cancel, recovery or halt-clear handler.

`PRAMANA_LEDGER_PATH` selects the existing paper ledger. `PRAMANA_OMS_DB` selects
the existing OMS; Compose now passes that same setting to the dashboard. Use an
absolute shared-volume path in containers so the engine and dashboard resolve
the same location. Unset remains unset; no account is silently enabled/migrated.

## Read-only and bounded inspection

Both SQLite connections open read-only, with query-only/trusted-schema settings
and a bounded lock wait. No database is created, schema migrated, account row
changed, recovery service invoked or model contacted by this reader. Normal
SQLite read locking/WAL sidecar behavior is not a hardware no-write guarantee.

The retained bound_v1 configuration hash and selected OMS path digest must agree.
Only selected-tenant order rows are read. Known states, units, timestamps and the
stored instrument-snapshot binding are checked before publication. Unsupported
schemas, missing/aliased storage, malformed rows and a replaced empty OMS alongside
existing paper fills are unavailable rather than an empty/clean account.

The reader limits each database file to 256 MiB and the selected inventory to
5,000 orders. Oversized input refuses. At most 50 open-order details are rendered;
the total pending count and an explicit truncation flag remain visible. Error
responses omit paths, SQL details, receipt contents and other tenants' identities.

## Deliberately limited meaning

An observed state is a snapshot of persisted local order projections, not a replay
of the full OMS event chain or reconciliation against every committed receipt.
Recovery audit counts are counts of stored rows, not reverified audit approvals.
The two stores are read under separate snapshots, not a cross-database transaction.
A path hash is not database authentication or evidence of market truth.

Every result explicitly states historyVerified=false, recoveryAuthorized=false
and liveExecutionAuthorized=false. Zero open order rows never clear a halt, pass a
launch gate or prove every broker/accounting obligation is resolved. The panel
is not added as an automatically passing readiness check. The existing reviewed
Python recovery API remains separate and still retains its durable halt.

The page shows the observation timestamp, requires refresh for another view, and
clears the previous result on failed refresh. Order details stack on narrow screens.
Cloudflare's separate hosted snapshot does not acquire local database access; the
panel is limited to the private local-file workspace.

## Verification

The new Node tests use real SQLite stores and exercise direct handler auth,
tenant isolation, malformed/absent state, limits, read-only contents and redacted
errors. Browser fixtures create disposable stores using the real Python paper
broker/OMS classes and a synthetic uncertain order; no fill is dispatched. The
browser journeys verify login, API-to-Activity data, absent write actions, denied
account overrides, mobile layout and unavailable refresh behavior. Existing
attribution/account/watchlist journeys continue to run; the external-account
row selector uses an exact name rather than matching another panel's label.

The first local browser attempt could not launch without the matching Chromium
binary. After installing it, the first runnable suite exposed an ambiguous older
INFY selector; that assertion was made precise rather than removed. Mobile
screenshot review also replaced a cramped table with wrapped order cards.
Exact-head test/build/CI evidence is retained in the PR certification comment.

## Release boundaries

The previously tool-blocked risk repair is not retried here. The four institutional
risk findings and their failing acceptance job are unchanged. Full institutional
daemon assembly, reviewed recovery/identity/migration, broader market/settlement
support, authentic feeds/broker evidence, target-host burn-in/restore, independent
security/human acceptance and forward after-cost effectiveness remain open.
No real account access grant, recovery, deployment, daemon restart or live-money
activation is performed by this change.
