# Cross-file paper recovery bundle

The recovery module captures the paper ledger, console database, XAI proofs, review artifacts, founder directives, operator halt file and optional research report as one explicitly selected bundle. It never discovers secret stores, stops services, activates a restored engine or overwrites an existing destination.

## Capture

Use `deploy/recovery-bundle.example.json` as a schema example. Set the exact deployed 40-character git revision, tenant and seven source paths. The ledger, console, proofs/reviews directories and directives must exist. An absent halt or research file is recorded as absent. Review selected paths to exclude credentials; the tool cannot infer whether arbitrary file contents contain secrets. Broker tokens, dashboard/review secrets, deployment origins, symbol/token mappings and other environment configuration must be retained separately in an approved encrypted secret/configuration store.

Halt entries, allow in-flight writes to settle, then stop **all writers**, including engine, dashboard/copilot, collector and any publisher or background process that writes selected files. Confirm their process/container states. The `--writers-stopped` option records an operator declaration; it is not proof of process shutdown. Capture from a maintenance process with access to the stopped volumes:

```sh
.venv/bin/python -m quant_ai.operations.recovery_bundle create \
  --source /path/to/reviewed-bundle-spec.json \
  --destination /path/to/new-bundle-directory --writers-stopped
```

The parent destination must exist. The destination itself must be new and outside selected source trees. Sources are fingerprinted before and after capture; any change rejects the bundle. SQLite backup API snapshots include WAL content and are normalized into standalone database files before hashing. Connections close before inventory capture, so transient WAL/SHM files do not become backup dependencies. Symlinks and non-regular files are unsupported.

The output includes `manifestSha256`. Retain this value **independently** of the bundle (for example, in the protected release evidence register). The manifest inventories every file, size/hash and directory, plus absent optional files and source fingerprint. Files are mode 0600 and directories 0700; these are permissions, not encryption. Store an encrypted off-host copy and test recovery without the original machine. Protect the receipt as well as the data. Fingerprinting detects ordinary capture-time changes; it does not establish a transaction across multiple databases or defend against coordinated adversarial source rewrites.

## Restore into an isolated directory

```sh
.venv/bin/python -m quant_ai.operations.recovery_bundle restore \
  --source /path/to/bundle-directory \
  --destination /path/to/new-isolated-restore \
  --manifest-sha256 INDEPENDENTLY_RETAINED_64_CHARACTER_SHA256
```

The command verifies the trusted manifest hash, full inventory and each source file, refuses unsafe paths/symlinks and verifies copied hashes. It then checks both SQLite databases, reconstructs paper accounting, counts console records and compares filled order IDs with JSON file-proof and tenant-scoped ledger protection-evidence order IDs. It preserves the halt file and database halt state. It does not connect to a broker, send a notification, restart a service or reset a halt.

- Exit 0 / `status: restored`: selected files passed the checks, internal accounting matched, and each recorded filled order had an order-ID reference in a file proof or valid ledger protection record.
- Exit 2 / `status: discrepancy`: the restored files are retained with a report so accounting or missing/invalid proof evidence can be investigated.
- Structural/checksum/schema failures abort and remove only the newly created partial destination. Existing paths are never replaced.

Proof coverage is an order-ID presence check, not verification of proof authenticity, tenant authorization, strategy quality or complete decision causality. Reviews are copied, not re-signed or extended; the application must still verify their release, configuration, signatures and expiry. Pending copilot requests require normal restart handling, and existing session cookies require the correct separately provisioned secret policy.

The coverage report counts ledger protection rows as `ledgerProtectionRecords`. Invalid file and ledger records are combined in `invalidJsonRecords`, replacing the earlier `invalidJsonFiles` field. [Protective-exit evidence](PROTECTIVE_EXIT_EVIDENCE.md) is deterministic execution evidence, not an AI decision trace.

Restored layout uses stable names `ledger`, `console`, `proofs/`, `reviews/`, `directives`, optional `halt` and `research`. Map application environment variables explicitly to these names in the isolated deployment. `restore-report.json` records the manifest digest, duration, counts, accounting result and proof gaps. Review it before activating anything.

## Remaining full recovery acceptance

A local file drill does not close target-host recovery. Verify encrypted off-host retrieval, fresh provider credentials, exact deployment configuration, dashboard login, saved lists/conversations, proof links, persistent halt, monitor alert/recovery, clock/calendar/feed coverage, process restart and rollback on the intended host. Measure recovery point and recovery time against agreed budgets. Never replace a current ledger with an older snapshot without independently reconciling intervening fills.

Local verification used isolated synthetic QA state. Ledger and console data, preferences, two conversations, five audit records and the halt file were restored. Accounting matched; one synthetic QA fill lacked an XAI proof and correctly produced `discrepancy` / exit 2. Tests separately cover a complete synthetic bundle, source-change rejection, file/manifest tampering, unsafe paths, symlinks and overwrite refusal. No external service was changed and no real recovery acceptance was signed.
