# Running strategy configuration evidence

Pilot factories now record the effective engine configuration in the paper ledger. The canonical JSON's SHA-256 identifies it; the dashboard no longer accepts a strategy review solely because two configured hashes agree. This is local process/ledger evidence, not remote attestation, a profitability test or immutable model-weight assurance.

## What is bound

The `pramana.runtime_strategy.v1` manifest contains the declared full release revision, all Python source files under `quant_ai` and their hashes, Python version and selected dependency versions. Its explicit built-in component fields include capital/sizing/Atlas/founder policies, scope and instruments, cadence/calendar/session definitions, stress/freshness/protective rules, friction/fees, provider chains and public-source identities, stream token-to-symbol configuration, specialist identities and inference model/timeout/transport kind. The supported SDK endpoint identity, retry/timeout settings, protective tenant/resolver identity and shared broker/feed wiring are also bound. Injected inference transports and custom protective resolvers remain incomplete.

Known credential attributes are excluded. Feed URLs are represented by hashes after removing user-info and common credential query parameters. Private founder instructions remain private configuration: keep the ledger and exported manifest access restricted. Arbitrary custom components remain `incomplete` until their configuration has an explicit descriptor; a class name alone does not qualify them.

Changing market ticks, HTTP cache/circuit counters and attribution observations do not change configuration. The adaptive weighting algorithm is covered by its component identity and source; actual weighted evidence and its weighting rationale remain in decision proofs. This is not a persisted adaptive-state recovery implementation or a full strategy-specific performance register. Per-call requested/returned model metadata and exact request/input fingerprints remain in [decision provenance](DECISION_PROVENANCE.md); a provider alias can still resolve to changed weights without changing the requested model string.

## Runtime and review rules

The manifest is checked after each protective sweep; Python source and selected package metadata are scanned at least every 60 seconds, and freshly before governed submission. Checks share the broker lock. A changed or unavailable manifest durably halts entries. Protective exits run first and remain permitted; a new manifest or restart does not clear a persisted fault halt. An incomplete initial configuration may collect unqualified paper evidence, but cannot pass the configuration/strategy review gates.

Canonical swarm fill/rejection evidence records the checked runtime summary. Distinct manifests are retained in `pilot_strategy_manifests`; historical records are not overwritten. The full table is included in ledger backups.

The dashboard requires a running heartbeat and manifest check within 10 seconds, source-check age at most 65 seconds, no unsupported fields/issues, startup/current hash agreement, matching release and an intact tenant-specific canonical registry record. Strategy acceptance must also match this active hash, the signed review artifact and `PRAMANA_STRATEGY_CONFIG_SHA256`. Existing review signature, release and expiry requirements still apply. Entry halt is a separate displayed check, so a healthy heartbeat cannot hide a halt.

## Operator workflow

1. Pin the actual release on the intended host. Set **the same** `PRAMANA_RELEASE_REVISION` (full 40-character git SHA) for engine and dashboard. Restarting with different source/configuration requires fresh review; do not edit code underneath a running engine. The manifest's revision is a deployment declaration; source hashes do not cryptographically prove the Git checkout or the already loaded Python bytecode.
2. Start the paper engine with its reviewed effective settings and inspect the Research configuration row. Missing/custom configuration stays unverified.
3. Export the actual recorded manifest, using a new output path:

```sh
python -m quant_ai.governance.runtime_manifest --ledger /data/pramana.db --tenant ghost --output /data/evidence/strategy-runtime-001.json
```

The directory must exist. The CLI prints the manifest hash and writes exact canonical bytes with mode 0600, refuses overwrite and validates the stored checksum. `--sha256 FULL_MANIFEST_HASH` exports a particular historical record; exporting it does not declare it active or approved.

4. Retain the manifest with the AI-specific evaluation and forward-paper evidence. Only after actual review, use its hash in `strategy_config_sha256`, configure the same dashboard hash, and create the signed strategy attestation described in [the runbook](PRIVATE_PILOT_RUNBOOK.md). Exporting a manifest signs nothing and clears no gate by itself.
5. After configuration/code changes, stop entries, investigate any persisted fault, restart from a pinned release and obtain a fresh review against its actual manifest. Follow the existing authorized resume procedure only after resolving the fault.

## Verification and limits

Regression coverage includes policy, source, fees, instrument mappings, holidays/sessions and specialist changes; credential rotation; unknown components; storage failure; exact private export; persistent entry halt with continued protective exits; and runtime binding inside a canonical governed fill. Dashboard tests reject missing, stale, changed, wrong-source, corrupt and cross-tenant records even when an attestation and environment hash agree.

These checks cover explicit built-in pilot configuration fields, not arbitrary callback closure contents, dynamically replaced methods, every possible custom component, the entire host environment, external broker lifecycle, deployment integrity against a privileged attacker, or AI effectiveness. Target-host source/configuration review, feed/token/soak/recovery/alert evidence, AI qualification and real operator sign-off remain required.
