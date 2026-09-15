# Private deployment feature wiring

Compose now passes the private research and company-event settings to the dashboard. Previously these variables could be present in `.env` but were omitted from the service, so their panels remained unconfigured despite valid source files. The isolated container verifier now uses the **resolved Compose environment** for the engine fixture and dashboard instead of independently reconstructing their settings.

## Configure the selected sources

| Variable | Consumer | Compose value / example |
|---|---|---|
| `PRAMANA_RESEARCH_LAB_REPORT` | Candidate comparison and export | `/data/research/comparison-001.json` |
| `PRAMANA_RESEARCH_DASHBOARD_SNAPSHOT` | Sanitized comparison/simulation/company-event panel | `/data/research/dashboard-snapshot.json` |
| `PRAMANA_PORTFOLIO_RESEARCH_REPORT` | Continuous replay, contribution and export | `/data/research/portfolio-001.json` |
| `PRAMANA_COMPANY_EVENTS_DB` | Company announcements, mapping and export | `/data/research/events.sqlite` |
| `PRAMANA_RESEARCH_REPORT` | Earlier deterministic research report | Defaults to `/data/research-report.json`; override now honored |
| `PRAMANA_REVIEW_DIR` | Release-bound operator attestations | Defaults to `/data/reviews`; override now honored |
| `PRAMANA_DIRECTIVES_HOST_FILE` | Host file mounted read-only for the engine | Absolute host path; defaults to the supplied example in `deploy/` |
| `PRAMANA_HOLIDAYS_JSON` | Engine, collector session label and risk-history calendar | Additive venue-to-date-list object, such as `{"INDIA":["2026-11-09"]}` |

The three private research/event settings remain empty by default. Publish actual reports for the configured tenant and populate the event database before enabling them. Paths for these dashboard settings refer to the filesystem **inside the container**, normally under the shared `/data` volume. A host filesystem path is not automatically mounted there. Restart the dashboard after changing its environment. Do not publish CI's synthetic fixtures to a real workspace.

The directives setting selects a host file, distinct from the engine's fixed `PRAMANA_FOUNDER_DIRECTIVES_FILE=/app/directives.json`. Relative host paths resolve from `deploy/`; an absolute path avoids ambiguity. The bind is read-only and refuses to create a missing source path. Provision a regular reviewed JSON file readable by container UID 10001. Inline directives remain a native-runtime option and are not injected by this Compose file.

## Calendar consistency

The collector now receives the same holiday override as the engine and uses that merged calendar for both its session label and completed risk-history dates. Overrides must be an object with string venues and lists of ISO date strings. Nulls, lists in place of the object, invalid venues and malformed date values fail; the collector validates before creating a broker client. The shared parser also governs engine startup.

Risk history still supports only the maintained 2026 calendar and documented Budget Sunday. Additional closures are explicitly recorded, hashed and labelled `operator_supplied_unverified`; this does not establish their correctness. Removing bundled closures or inventing special sessions is unavailable for this risk input until qualified. A provider bar on a declared closed date is retained so the downstream reader can reject the conflict, rather than silently removing it. The engine's runtime manifest fingerprints its effective calendar as before.

## Recovery inventory

The example schema-2 inventory now explicitly includes `/data/market-monitor.json`, which contains the history needed to reproduce private risk reports, alongside the research journals/reports and event database. Update every selected source path when overriding the dashboard settings. Retain the actual source journals, provider receipts and original inputs, not only their published summaries. Stop the collector along with every selected writer before capturing this file. Review custom directives at their mounted container location, and retain deployment settings/secrets separately in the approved protected store.

This example is not automatic discovery or proof of complete deployed coverage. Missing selected sources must be investigated. Do not remove entries merely to make a backup pass. [Recovery contract](RECOVERY_BUNDLE.md).

## Verification scope

The isolated fixture creates an actual research journal with deliberately missing decisions, a continuous journal with independently checkable ₹720 cash / three remaining shares / ₹1,050 equity, and imported company disclosure/mapping records. Python publishers generate the reports; the production dashboard parses and exports them. Comparison completeness, source qualification and operator acceptance remain failed/unverified.

The runtime verifier checks the resolved source paths, custom read-only directives mount, actual ₹123,456 starting capital and three-position limit, all three authenticated exports, persistence across restart, and fail-closed behavior when configured files disappear. It restores those files and verifies recovery. Both runtime containers have networking disabled. The earlier login, account, watchlist, halt, post-start heartbeat and staleness checks remain in place. Results and image IDs are in the CI `pilot-container-verification` artifact.

This verifies wiring and isolated behavior. It does not deploy the real Compose stack, qualify market/source rights, obtain real strategy evidence, operate an external broker, deliver an alert, renew a real token or perform off-host recovery. Those launch gates remain open.
