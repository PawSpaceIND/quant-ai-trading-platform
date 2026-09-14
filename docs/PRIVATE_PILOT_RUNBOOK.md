# Private paper pilot deployment and recovery

For the separate offline candidate lab, publish a sanitized report with
`python -m quant_ai.research.workspace_report` and set `PRAMANA_RESEARCH_LAB_REPORT`
only on this authenticated workspace. [Publication and comparison workflow](research-evaluation-lab.md#private-research-workspace-integration).
The report remains insufficient evidence and never grants strategy acceptance.

This release is scoped to a founder-only INR / NSE cash-equity and ETF account. It is not a public SaaS release or a live-money adapter. Do not reuse a ledger containing USD trades. Use a dedicated new account and volume. A dashboard observation list does not authorize engine instruments.

## Configure and start

Review [deployment feature wiring](DEPLOYMENT_WIRING.md) for the private research/event paths and custom directives mount. These settings are now propagated by Compose; report paths refer to the shared container volume, while `PRAMANA_DIRECTIVES_HOST_FILE` refers to a host file. The collector and engine now share validated holiday overrides.

1. Copy `.env.example` to `.env` (mode 0600). Set a randomly generated dashboard secret of at least 32 characters, Claude credentials, licensed Zerodha credentials, instrument tokens and the exact symbol map. Review `deploy/founder-directives.example.json`; the supplied pilot watches INFY only. Map every configured instrument. Set the public origin to the exact browser origin.
2. Supply the actual exchange holiday calendar using `PRAMANA_HOLIDAYS_JSON`. Missing macro/fundamental feeds remain unavailable and may cause abstention; do not add synthetic constants to force trades.
3. From `deploy/`, run `docker compose --env-file ../.env -f docker-compose.yml up -d --build`. The daemon, collector and dashboard share the same data volume and UID. The dashboard binds only to host loopback. Access it over an SSH tunnel, or a private HTTPS reverse proxy with an exact `PRAMANA_PUBLIC_ORIGIN` and authenticated network access. Do not open the port publicly.
4. Confirm the signed login session, current protection heartbeat, currency scope, tick coverage, and matching ledger/valuation version. Test during an exchange session. A green HTTP response or running container alone is insufficient.
5. Configure an **independent host/container monitor** to alert the operator when the engine health check fails or market observations become stale. An alert emitted by the stopped engine itself is not independent monitoring. Record an actual received alert during a failure drill. No external notification is sent by setup commands in this repository.

The existing optional Telegram adapter remains supported through local environment settings. To use it in Compose, explicitly add `PRAMANA_TELEGRAM_BOT_TOKEN` and `PRAMANA_TELEGRAM_CHAT_ID` through the host secret mechanism after approving the destination. Do not place credentials into source or screenshots.

## Halt and restart

Before qualifying a release, inspect its `containers` CI artifact. The [image and health verification](CONTAINER_VERIFICATION.md) builds both deployment images and exercises isolated shared-state/authentication/restart flows. It does not replace starting the real deployment. The local health command now exits 2 for a halt or invalid engine payload, even with a recent database timestamp; a live halted protection loop is reported separately. Do not automatically restart or resume from this availability signal.

The dashboard can request a halt only. Confirm acknowledgement from the independent protection heartbeat; a stopped process cannot acknowledge it. Protective exits continue while halted. Use the existing `pramana halt` and `pramana resume` operator commands against the same paths. Investigate a fault before resuming. Persistent fault halts must not be cleared merely to make readiness indicators green.

The protection worker is independent of the AI event loop but shares the process and ledger. A host crash or disk failure can still stop protection. Paper stops are simulated, and their trigger prices are not guaranteed execution prices.

## Backup and non-destructive restore drill

Inside the engine container, with the shared volume mounted:

```sh
python /app/scripts/pilot_ops.py backup --database /data/pramana.db --destination /data/backups/ledger-YYYYMMDD.sqlite
python /app/scripts/pilot_ops.py backup --database /data/pilot-console.sqlite --destination /data/backups/console-YYYYMMDD.sqlite
python /app/scripts/pilot_ops.py restore-drill --database /data/backups/ledger-YYYYMMDD.sqlite --destination /data/drills/ledger-YYYYMMDD.sqlite
```

Use unique paths; commands refuse overwrite. SQLite's backup API captures a consistent database including WAL changes. The drill validates the recorded SHA-256 and database integrity and writes a separate database. It does not replace the active ledger. Back up the XAI proof directory and acceptance artifacts alongside the databases, store an encrypted copy outside the host, and test restoration of the **whole bundle** on a separate deployment. Halting and stopping writers makes the ledger/proof/console bundle easier to reconcile. Record counts, latest order IDs, settings, halt state, recovery duration and the restored dashboard checks. An individual SQLite integrity check is only part of recovery acceptance.

## Rollback

Pin images to a reviewed commit/digest before deployment. Halt entries, stop writers, create a complete backup bundle, and record the current revision. Prefer rollback of application images while retaining compatible data. Never restore an older ledger over newer fills without explicit reconciliation. If migration compatibility is uncertain, restore into an isolated volume and review there first.

## External launch gates

Real session data coverage, token renewal, target-host startup, an independently received alert, complete restore/rollback, and strategy holdout/forward evidence remain required. Record them in the closure register. Unit tests and synthetic browser fixtures do not satisfy these gates. Container execution has to be verified on a Docker-capable host; source inspection is not a passed deployment.

## Reproducible baseline research

Prepare licensed and corporate-action-consistent CSV data with increasing timezone-aware `timestamp` and positive `close` columns. Run:

```sh
python -m quant_ai.validation.experiment --data prices.csv --output experiment-001.json --cost-bps 10
```

The baseline selects among four moving-average windows using training data, evaluates disjoint forward windows, freezes the final selection before a held-out interval, and compares that holdout to buy-and-hold and cash. It also runs a seeded moving-block bootstrap of return paths and drawdowns. The price series is interpreted at its supplied interval; no daily annualization is invented. This is a causal signal baseline with idealized close-to-close marks and a configurable turnover-cost assumption, not an intrabar execution simulation or validation of the AI swarm. Cost sensitivity and precise execution parity remain additional experiments.

Outputs refuse overwrite and include data, code and report hashes, trial count, settings and limitations. Keep every experiment (including failed ones) in an append-only experiment register; trying new settings after inspecting the holdout consumes that holdout. Reserve fresh unseen data for final evaluation. The module cannot enforce research discipline outside its process.

Set `PRAMANA_RESEARCH_REPORT` to a reviewed report path to expose it in the dashboard. Publication does not approve strategy promotion; forward-paper, deployment and independent review gates remain separate. No test-generated report should be published as investment evidence.

## Recording external acceptance

The `deploy/review-*.example.json` templates deliberately start incomplete. An operator must fill them from retained evidence, reference the evidence bundle, and identify the exact deployed git revision. Strategy review requires AI-specific holdout, costs, trial accounting and calibration review, plus the existing minimum trade/history/expectancy/drawdown/profit-factor/regime policy. The deterministic baseline report cannot satisfy this schema. The numeric minimums are filters, not a profit guarantee or a substitute for expert review.

After the checks have actually been performed, an authorized reviewer can record an attestation:

```sh
python -m quant_ai.governance.pilot_review --artifact completed-recovery.json --gate recovery --tenant ghost --revision FULL_GIT_SHA --reviewer REVIEWER_NAME --output /data/reviews/recovery.json
```

Use the corresponding strategy artifact and gate for `/data/reviews/strategy.json`. Export the engine’s actual [runtime strategy manifest](RUNTIME_STRATEGY_MANIFEST.md), archive it with the reviewed feature/input and model evidence, record its SHA-256 in `strategy_config_sha256`, and set the dashboard’s `PRAMANA_STRATEGY_CONFIG_SHA256` to that hash. Both engine and dashboard require the same pinned `PRAMANA_RELEASE_REVISION`. Fresh engine evidence and its intact registry record must match the signed review; manually copying a hash into dashboard settings is insufficient. A configuration mismatch keeps strategy acceptance unverified. Set a separate `PRAMANA_REVIEW_SECRET` of at least 32 characters in the signing environment and dashboard, along with `PRAMANA_RELEASE_REVISION` matching the deployed image/source. Use the host secret store; no private review key belongs in source. Custom deployments can set `PRAMANA_REVIEW_DIR`.

Attestations contain the reviewed artifact, its hash, reviewer label, tenant and release; they expire after seven days. The dashboard verifies the HMAC, scope, revision and expiry. It also requires 30 qualified recorded paper sessions before the forward-observation row passes. This proves that a key holder recorded the review, not that the named person’s identity, underlying market evidence or future returns were independently certified. A changed release requires a new review. Keep previous signed records in the evidence archive before publishing the new active record. There is no endpoint that enables live orders, even after every pilot row passes.

The hosted Worker now provides a credential-isolated `/healthz` probe for independent monitoring. Follow [setup and failure-drill instructions](INDEPENDENT_MONITOR.md). This checks original engine evidence, not just upload time; local tests do not satisfy the actual received-alert gate.

For a single manifest spanning databases, proofs, reviews, directives and halt state, use the [cross-file recovery bundle workflow](RECOVERY_BUNDLE.md) with `deploy/recovery-bundle.example.json`. It requires stopped writers, records absent optional artifacts, retains a trusted manifest digest separately, restores to a new directory and checks internal accounting plus order-ID proof coverage. It complements the target-host recovery/rollback drill; it does not replace it.

Before recording strategy acceptance, generate and retain [completed-trade episode evidence](TRADE_EPISODE_EVIDENCE.md). Count flat-to-flat episodes, not fill rows; check that the evaluated episodes belong to the frozen strategy and reconcile the review's claimed metrics to the retained artifacts. The dashboard requires at least 100 strategy-linked completed episodes and 30 configuration-qualified days, with no unresolved coverage. Export the [strategy-specific evidence](STRATEGY_EPISODE_ATTRIBUTION.md), put its `evidenceSha256` into `strategy_evidence_sha256`, and reconcile the signed trade count, mean P&L and profit factor to that exact report. Any changed evidence snapshot needs a fresh review.


## Continuous research publication

Follow the [portfolio research workspace workflow](PORTFOLIO_RESEARCH_WORKSPACE.md) to export an existing continuous journal to a new private report and configure `PRAMANA_PORTFOLIO_RESEARCH_REPORT`. This adds gap-aware candidate curves, holdings/orders, authenticated export and bounded Atlas context. List all experiment/portfolio/event journals, provider receipt directories, published reports and raw input archives in the [schema-2 recovery inventory](RECOVERY_BUNDLE.md). Generic directories do not receive SQLite backup semantics; use the explicit journal kinds. Local restoration is verified, while encrypted off-host and target-host recovery, input qualification and operator acceptance remain required.


## Private company announcements

Configure `PRAMANA_COMPANY_EVENTS_DB` with an absolute path to the existing three-table company-event database and include it as `company_events` in the schema-2 recovery inventory. Follow [the company announcement workflow](COMPANY_EVENT_WORKSPACE.md) for Markets search/watchlist, dated revisions, capture failures, mapping review, historical export and scoped Atlas requests. Refresh only reads stored evidence; use the existing `research_extensions.py event-fetch` collector separately. The founder session can append mapping assertions at server time after reviewing the exact instrument reference. Do not share the selected database across tenants with different rights. Mapping withdrawal and later re-review are now supported; follow [the lifecycle instructions](COMPANY_MAPPING_LIFECYCLE.md). No collector scheduler is supplied by this UI. Qualify real target-host collection, coverage, instrument mappings and source rights before relying on this evidence.


## Recorded account contribution

The private Portfolio page automatically reads the matching engine valuation and account ledger for [instrument/cost contribution](PAPER_ACCOUNT_CONTRIBUTION.md). No separate publication job or migration is required. Restart the private UI after deploying this code. New fills require a matching engine valuation; missing/stale/inconsistent records remain explicit. Export the private report at `/api/portfolio/contribution` and retain it with the already selected operational ledger/valuation recovery inventory. It is excluded from cloud snapshot publication. This report does not replace strategy evidence or operator acceptance.


## Historical risk workspace

Update the collector and private dashboard together. The existing `PRAMANA_MARKET_SNAPSHOT` path supplies the new risk-history dataset; old snapshots display unavailable. History now covers the maintained 2026 calendar and the documented Budget Sunday, while provider adjustments and full calendar/source qualification remain explicit gaps. Review the new special-session fingerprint in any deployment acceptance. Retain the original market snapshot in the selected recovery/input inventory if risk reports must be reproduced. Private `GET /api/portfolio/historical-risk` exports the calculated report; it does not replace that source archive. [Data contract, limits and verification](HISTORICAL_PORTFOLIO_RISK.md).

## Stored protection qualification

Before authorizing new pilot entries, confirm **Stored position protection** is current and complete in Research → Pilot readiness, in addition to heartbeat, marks and reconciliation. A missing/invalid level or failed protective exit latches a durable entry halt; fixing the data or restarting does not clear it. Review the raw affected records and canonical fills before using the existing controlled reset workflow. Do not invent a stop to clear a gate. Legacy symbol-only scope requires the normal pilot startup with explicit instrument configuration before buying. See [stored protection contract](PROTECTION_COVERAGE.md).

## Invalid ledger observation

Treat **Portfolio totals withheld** or runtime `valuation.status = unavailable` as an accounting/mark fault, even when protection heartbeat is current. Keep entries halted. Preserve the affected raw state and inspect the bounded reason, canonical fills, account/cost records and source observations. Do not round quantities, fabricate cost basis or clear the halt to make the dashboard green. Use the existing reviewed recovery workflow if authoritative repair cannot be established.

Valid holdings may still exit independently, but invalid cash blocks every fill; corrupt capital/schema/scope may prevent startup entirely. After reviewed recovery, verify a fresh complete valuation, coverage and reconciliation before considering the controlled halt-reset workflow. Invalid minutes remain in the curve and their day cannot qualify as clean forward evidence. Updating both engine and private UI is required for this contract. See [ledger integrity and precise limits](LEDGER_INTEGRITY.md).
