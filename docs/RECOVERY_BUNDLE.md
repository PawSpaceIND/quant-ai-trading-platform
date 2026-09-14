# Cross-file paper recovery bundle

The recovery module captures the paper ledger, console database, XAI proofs, review artifacts, founder directives, operator halt file and optional research report as one explicitly selected bundle. Schema 2 also supports explicitly inventoried experiment journals, continuous portfolio journals, company-event databases, provider receipts, published reports and input archives. It never discovers secret stores, stops services, activates a restored engine or overwrites an existing destination.

## Capture

Use `deploy/recovery-bundle.example.json` as a schema example. Set the exact deployed 40-character git revision, tenant and seven core source paths, then review every `research_state` entry for the actual deployment. The ledger, console, proofs/reviews directories and directives must exist. An absent halt or research file is recorded as absent. Review selected paths to exclude credentials; the tool cannot infer whether arbitrary file contents contain secrets. Broker tokens, dashboard/review secrets, deployment origins, symbol/token mappings and other environment configuration must be retained separately in an approved encrypted secret/configuration store.

Halt entries, allow in-flight writes to settle, then stop **all writers**, including engine, dashboard/copilot, collector, research/provider workers, event fetchers and any publisher or background process that writes selected files. Confirm their process/container states. The `--writers-stopped` option records an operator declaration; it is not proof of process shutdown. Capture from a maintenance process with access to the stopped volumes:

```sh
.venv/bin/python -m quant_ai.operations.recovery_bundle create \
  --source /path/to/reviewed-bundle-spec.json \
  --destination /path/to/new-bundle-directory --writers-stopped
```

The parent destination must exist. The destination itself must be new and outside selected source trees. Sources are fingerprinted before and after capture; any change rejects the bundle. SQLite backup API snapshots include WAL content and are normalized into standalone database files before hashing. Connections close before the copied-file inventory is recorded, so WAL/SHM files do not become backup dependencies. A read-only open can create an empty source WAL; zero-byte sidecars contain no frames and do not count as a data change. Nonempty source WALs remain fingerprinted. Symlinks and non-regular files are unsupported.

The output includes `manifestSha256`. Retain this value **independently** of the bundle (for example, in the protected release evidence register). The manifest inventories every file, size/hash and directory, plus absent optional files and source fingerprint. Files are mode 0600 and directories 0700; these are permissions, not encryption. Store an encrypted off-host copy and test recovery without the original machine. Protect the receipt as well as the data. Fingerprinting detects ordinary capture-time changes; it does not establish a transaction across multiple databases or defend against coordinated adversarial source rewrites.

## Restore into an isolated directory

```sh
.venv/bin/python -m quant_ai.operations.recovery_bundle restore \
  --source /path/to/bundle-directory \
  --destination /path/to/new-isolated-restore \
  --manifest-sha256 INDEPENDENTLY_RETAINED_64_CHARACTER_SHA256
```

The command verifies the trusted manifest hash, full inventory and each source file, refuses unsafe paths/symlinks and verifies copied hashes. Schema 2 also checks each selected research database and compares its logical state and supported deterministic reports with the captured manifest. It then checks both core SQLite databases, reconstructs paper accounting, counts console records and compares filled order IDs with JSON file-proof and tenant-scoped ledger protection/swarm-evidence order IDs. It preserves the halt file and database halt state. It does not connect to a broker, send a notification, restart a service or reset a halt.

- Exit 0 / `status: restored`: selected files passed the checks, internal accounting matched, and each recorded filled order had an order-ID reference in a file proof or valid ledger protection/swarm record.
- Exit 2 / `status: discrepancy`: the restored files are retained with a report so accounting or missing/invalid proof evidence can be investigated.
- Structural/checksum/schema failures abort and remove only the newly created partial destination. Existing paths are never replaced.

Proof coverage is an order-ID presence check, not verification of proof authenticity, tenant authorization, strategy quality or complete decision causality. Reviews are copied, not re-signed or extended; the application must still verify their release, configuration, signatures and expiry. Pending copilot requests require normal restart handling, and existing session cookies require the correct separately provisioned secret policy.

The coverage report counts ledger protection rows as `ledgerProtectionRecords` and governed swarm-fill rows as `ledgerDecisionRecords`. Invalid file and ledger records are combined in `invalidJsonRecords`, replacing the earlier `invalidJsonFiles` field. [Protective-exit evidence](PROTECTIVE_EXIT_EVIDENCE.md) is deterministic execution evidence, distinct from [governed swarm-fill traces](SWARM_FILL_EVIDENCE.md). Both canonical tables are captured inside the ledger backup.

Restored research entries use `research-state/<selected-name>`; files have no inferred extension. Restored core layout uses stable names `ledger`, `console`, `proofs/`, `reviews/`, `directives`, optional `halt` and `research`. Map application environment variables explicitly to these names in the isolated deployment. `restore-report.json` records the manifest digest, duration, counts, accounting result and proof gaps. Review it before activating anything.

## Remaining full recovery acceptance

A local file drill does not close target-host recovery. Verify encrypted off-host retrieval, fresh provider credentials, exact deployment configuration, dashboard login, saved lists/conversations, proof links, persistent halt, monitor alert/recovery, clock/calendar/feed coverage, process restart and rollback on the intended host. Measure recovery point and recovery time against agreed budgets. Never replace a current ledger with an older snapshot without independently reconciling intervening fills.

Local verification used isolated synthetic QA state. Ledger and console data, preferences, two conversations, five audit records and the halt file were restored. Accounting matched; one synthetic QA fill lacked an XAI proof and correctly produced `discrepancy` / exit 2. Tests separately cover a complete synthetic bundle, source-change rejection, file/manifest tampering, unsafe paths, symlinks and overwrite refusal. No external service was changed and no real recovery acceptance was signed.


## Explicit research inventory (schema 2)

`research_state` maps up to 64 names to `{ "kind": "...", "path": "/absolute/source" }`. Names use lowercase letters, digits, underscores and hyphens and begin with a letter. All selected research entries must exist; unlike the two legacy optional files, they cannot silently become absent. Unknown specification fields, unsupported kinds, overlapping sources, symlinks and source paths inside the destination are rejected. Add separate entries for every journal/report used by the deployment; nothing is discovered automatically. Remove a sample entry only after confirming it is unused, not because its file is unexpectedly missing.

| Kind | Capture and verification | Typical selection |
|---|---|---|
| `experiment_journal` | SQLite snapshot, integrity/foreign-key checks, exact table set, all stored rows, evidence and deterministic report hashes for every experiment | Frozen cases, outcomes and model decisions |
| `portfolio_journal` | SQLite snapshot, integrity/foreign-key checks, exact table set, all stored rows, source evidence and deterministic replay hash | Continuous quotes, orders, clocks and candidate books |
| `company_events` | SQLite snapshot, integrity/foreign-key checks, exact table set and all-row hash including raw BLOB bytes | Feed captures/failures, revisions and reviewed mappings |
| `file` | Byte-preserving copy and manifest size/hash | Published reports, input/configuration files without secrets |
| `directory` | Complete directory/file inventory and byte hashes | Provider receipt files and retained raw input archives |

Generic files/directories are preserved by hash; their contents are not semantically validated. Select SQLite journals using their database kinds, rather than burying databases inside a generic directory. The company-event check proves preservation of captures/mappings, not feed authenticity, parser correctness, licensing or comprehensive event coverage. The experiment and portfolio report checks use the installed research code; restore with the captured release. A changed replay result aborts restoration instead of silently accepting drift.

The source fingerprint covers the core and selected research files together before and after capture. Stopped writers remain required: this is not a transaction across databases and receipt files. Logical database hashes retain duplicate rows, schema and raw capture bytes. Reports contain hashes/counts, not raw source packets, feed text or provider responses. The private bundle itself contains those selected raw records and must be encrypted before off-host storage.

`restore-report.json.researchRecovery` reports `selected_state_verified` with per-source evidence, or `not_selected` when no research state was listed. Existing schema 1 bundles are still restorable but cannot imply research coverage. `status: restored` proves only the selected checks; it does not prove the inventory was complete for a deployment. Neither restoration nor verification retries pending provider calls, republishes reports, resets halts, signs acceptance or starts an engine.

After restoration, explicitly map `PRAMANA_RESEARCH_LAB_REPORT` and `PRAMANA_PORTFOLIO_RESEARCH_REPORT` to the selected report files. Map research CLI database and receipt paths to their restored entries before a reviewed resumption. Preserve pending receipts: their existing exclusive-create guard must continue blocking an automatic paid retry. Retain source code/dependencies, private input archives, the trusted manifest receipt, deployment configuration and secret provisioning separately as required by the full recovery plan. A report referring to an older experiment snapshot still needs that older source snapshot archived; selecting only today's journal cannot reconstruct its prior state.

## Research recovery verification — 14 September 2026

The combined Python suite passes **500 tests** with two existing Starlette deprecation warnings; Ruff passes. The focused recovery suite covers 15 additional cases, including all three research stores, published files, pending receipts, committed WAL frames, capture-time writes, missing/overlapping/misnamed sources, wrong database kinds, foreign-key gaps, raw-capture tampering, missing receipts, corrupt event sequence, replay drift and legacy bundles. The old paper fixture now explicitly closes its connections; this exposed and fixed the empty-WAL false rejection above.

A separate local CLI create/restore drill restored six selected research sources. Original and restored published files were byte-identical and both loaded as `published` through the existing TypeScript dashboard readers; a missing portfolio valuation remained null. Independent fixture arithmetic retained ₹720 cash, ₹20 realized P&L and three shares, with stale current equity unavailable. The restored company mapping became available only after its recorded verification time. A pending Claude receipt prevented `evaluate_case` from issuing a provider request. No network/provider request, order, source fetch, service activation or acceptance occurred in the drill.

The local verification artifact retains the synthetic manifest digest and exact tested revision. This is engineering evidence, not encrypted off-host recovery or target-host qualification. Those requirements remain open.


Company mapping withdrawals use a reserved empty-symbol journal row without changing the three-table schema. Schema-2 recovery includes this row in its all-row hash. The lifecycle regression restores withdrawal history unchanged and confirms that current NSE sources stay excluded while pre-withdrawal historical sources remain available. [Operator and rollback compatibility rules](COMPANY_MAPPING_LIFECYCLE.md). This is a local regression, not off-host recovery qualification.


The deployment example now explicitly selects the source market snapshot as a schema-2 generic file, preserving the history behind private risk reports. Custom dashboard/report/event paths must be reflected in the selected inventory, and the collector must be stopped for capture. This does not add automatic inventory discovery or qualify off-host recovery. [Feature/source configuration](DEPLOYMENT_WIRING.md).
