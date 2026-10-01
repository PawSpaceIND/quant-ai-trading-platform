# Guarded offline PAPER release interface

`pramana-paper-release` (or `python -m quant_ai.operations.paper_release_cli`) defaults
to `qualify`. Qualification and `plan` use existing private databases in `mode=ro`,
query-only read transactions, with no constructors, schema creation, network clients
or process control. Output is a sanitized JSON summary: hashes, status and row counts,
never raw historical evidence, cash/positions, prompts, credentials or environment.
An unknown argument/value produces a fixed refusal without echoing pasted input.

This is not deployment, runtime activation, external identity authentication or an
inverse migration. It never stops a service, clears a halt/reservation, subscribes,
places an order, trains a model or changes capital/risk/spend settings. Installation
is not authorization to execute a real host transition. The AWS owner's reviewed
host qualification, writer inventory and protection interruption window remain
prerequisites. The process can run offline against isolated rehearsal databases.
Path-bound certificates from rehearsal copies must not be copied into different
live paths as a migration shortcut.

## Read-only inputs

Every command requires `--ledger`, `--oms`, `--catalog`, `--tenant` and
`--account-binding`. Catalog is a private, nonsymlink, single-link JSON file:

```json
{"schema":"pramana.paper_release_catalog.v1","instruments":[
  {"symbol":"INFY","market":"INDIA","asset_class":"EQUITY",
   "currency":"INR","exchange":"NSE","tradable":true}
]}
```

This example is synthetic, not an authorized host catalog. Exact file bytes supply
the catalog capture SHA256; account binding is a reviewed64-hex declaration. Neither
hash authenticates a catalog, broker account or source. Unknown fields, duplicates,
observation-only/derivative/non-NSE identities and catalogs exceeding50 refuse.
Existing library schema, full holding-key, evidence, accounting and protection
qualification remain mandatory; the interface does not repair unsupported stores.

`qualify` reports whether a trusted fresh empty OMS still requires explicit identity
provisioning. `plan` requires that provisioning already exist and reports only the
exact transition-plan SHA256 and sanitized qualification metadata. The underlying
library's immutable plan retains `apply_supported=false`: that raw plan is not an
unguarded apply permission. The supported operator write path is the separately
reviewed preflight below, not a direct call to the primitive.

After transition, default `qualify` verifies the existing certificate/bindings and
reports reconciliation/protection plus forward-recovery-only status without expecting
an empty OMS. It does not verify every later order's OMS linkage or authorize entries.
New `plan` refuses a committed transition. No delete/reset/restore command exists.

## Explicit write preflight

Only `provision-oms` and `apply` write. They require ALL of:

- `--preflight FILE` and `--reviewed-preflight-sha256` matching exact reviewed bytes;
- `--ack-forward-only`, `--ack-quiesced-writers`, `--ack-protection-interruption`;
- explicit effective guards: `TRADING_LIVE_MONEY_ACTIVE=false`,
  `PRAMANA_IBKR_ENABLED=false`, `PRAMANA_PAPER_OPPORTUNITY_SELECTION=false`,
  `PRAMANA_PILOT_MODE=true`. Missing/unknown values refuse; nothing is set by the CLI;
- `--reviewed-empty-oms-sha256` for provision or `--reviewed-plan-sha256` for apply;
- fresh stopped-writer/protection attestations and the complete verified backup set.

The private preflight JSON has exactly these fields (substitute reviewed values):

```text
schema: pramana.paper_release_preflight.v1
mode: PAPER
live_money_active: false
ibkr_enabled: false
selector_enabled: false
runtime_state: STOPPED
writers: {engine: STOPPED, protection: STOPPED, collector: STOPPED,
          dashboard: STOPPED, tokenwatch: STOPPED, operator_jobs: STOPPED}
protection_interruption_reviewed: true
captured_at: aware UTC timestamp, not future
expires_at: aware UTC timestamp, at most15 minutes after capture
release_revision: reviewed full40-hex source revision (declaration, not image proof)
account_binding: exact reviewed account SHA256 declaration
catalog_capture_sha256: exact reviewed catalog file SHA256
ledger_schema_sha256: exact schema hash from read-only qualification
lease_file: existing private nonsymlink operator lease file, distinct from databases
lease_sha256: exact reviewed lease contents SHA256
stores: {ledger: path, oms: path, ai_budget: path, ai_spend: path, console: path}
backup_manifests: {ledger: path, oms: path, ai_budget: path, ai_spend: path, console: path}
```

The operator inventory must include ALL writers, not just the named primary service.
These fields are reviewed attestations, not independent process-shutdown evidence.
The CLI reports `quiescence_attested=true`, `process_shutdown_verified=false`.
An advisory lease excludes another cooperating operator. SQLite write transactions
exclude competing database writes while the operation runs; neither proves that
an external process has stopped or cannot resume after locks are released.

Every store must be an existing distinct private regular single-link file. Missing
stores are not created. Backups must be distinct private files, checksum/integrity
verified, whose complete logical schema/rows still equal each current source.
Use the existing `scripts/pilot_ops.py backup` online SQLite method with new paths;
do not raw-copy an active WAL `.db` alone. The manifests must have the existing
source/backup/sha256/created_at/integrity fields. Cross-store capture requires the
reviewed writer-quiescence boundary; individual backups are not cross-store atomic.
Separate off-host custody and isolated restore are operational requirements that
these local checks do not independently authenticate.

The CLI holds the lease and write locks on all related stores that the primitive
does not itself own. It revalidates the complete backups before mutation, checks
expiry again, and supplies full ledger/OMS snapshot hashes to the primitive. Those
hashes are checked inside the primitive's existing write transactions, preventing
a write between backup validation and primitive admission from silently invalidating
the recovery boundary. No original mutation, replay or inverse semantics change.
The reviewed time window is also passed into both primitives: it is checked after
their write locks are acquired and immediately before commit. Expiry during schema
or conservation validation rolls back UUID/certificate/pin/holding changes; the
CLI's earlier check alone is not admission for a later commit.

Provisioning creates only the already-reviewed immutable internal OMS UUID. It is
not a broker credential and does not bind ledger holdings. After provisioning, take
a NEW OMS backup, refresh/review the preflight and generate/review the exact new plan
before apply. Old pre-provision backups/plans cannot authorize apply. Apply records
the existing immutable certificate/pin and allowed holding identity changes only;
all original global-row conservation/reconciliation/protection guards still execute.

## Recovery and release boundaries

On refusal, stop and investigate; no reset or automatic repair is offered. A caught
precommit failure rolls back. If command interruption makes commit uncertain, use
read-only qualification and exact current backups; do not repeat an unknown write
or restore an earlier ledger over newer activity. Provisioned-only interruption is
not a completed transition. A committed transition/later fill requires reviewed
forward recovery preserving latest ledger, OMS and immutable certificate. An older
image/data restore is not automatically compatible. Successful JSON means the
specified offline operation completed, not that a daemon is started or ready.

Compose now forwards `PRAMANA_PAPER_OPPORTUNITY_SELECTION` ONLY to the PAPER engine,
with default `false`. This commit sets no host value. Unknown/empty values and all
existing bound-paper/pilot/catalog/subscription/history/freshness gates retain their
builder refusal behavior. Review effective container settings before release;
installation must not inadvertently inherit an existing opt-in environment value.
Identity migration and selector activation remain separate reviewed operations.

Executable synthetic acceptance covers read-only/default/redacted planning, exact
review/expiry/mode/quiescence/backup refusals, lock contention and raced writes,
fresh/idempotent provision, required post-provision backup/review, allowed apply and
restart/read-only recovery, isolated restore without source overwrite, and default-off
Compose wiring. It does not prove actual AWS data coverage, process shutdown, model
skill, profitable paper results or live-money readiness.
