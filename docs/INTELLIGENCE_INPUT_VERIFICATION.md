# Production intelligence inputs: configuration is not live acceptance

## Exact source path

`build_ghost_runner_from_env()` calls `_env_intelligence_providers()` and passes
its news, fundamental and macro wrappers to the runner. Unconfigured news/macro
or a failed adapter returns empty observations; this path has no sandbox fallback.
The low-level runner and demo CLI keep synthetic defaults for explicit tests.
Do not classify a manually assembled runner from its name or heartbeat alone.

## Read-only configuration diagnostic

After this code is reviewed and available in the chosen source tree:

```bash
python -B scripts/inspect_intelligence_inputs.py
```

For an owner-approved installed container, execute the same command inside that
container so it inherits that container's environment. Do not paste `.env`, keys,
feed URLs, broker session files or the full environment into chat. This command
has no provider-probe or clock-override argument. It imports the production
factory, constructs adapters and reports fixed adapter labels only; it does not
fetch a URL, contact a model/broker, create a runtime, change data or restart a
service. Factory construction can read the configured founder-directives file.
It does not open credential files or automatically source `.env`.

Output `CONFIGURATION_ONLY` distinguishes `configured_unverified` from
`not_configured`; `data_availability` and `freshness` remain `not_probed`.
`running_process_verified`, `source_authenticity_verified` and
`trading_authorized` stay false. Even inside the container, this separate process
is a configuration preview, not inspection of the engine's in-memory objects.
Unknown wrappers/adapters or invalid settings refuse without printing private
exception details. This diagnostic cannot clear a halt or approve a model.

## Macro observation-clock correction

FRED's adapter previously labelled a multi-indicator snapshot using the newest
component date. Thus an older component could inherit another series' freshness.
A separate oldest-component freshness date is now carried alongside the existing
latest-observation clock. The latter still drives the unchanged change-history
calculation. Both pipeline paths use the conservative date for freshness, and
model evidence names the oldest date separately without relabelling its existing
latest-observation field. Legacy two-field snapshots remain compatible. Non-finite
values, future observations, naive clocks and noncanonical observation dates
refuse. Empty/partial input remains empty/partial; the existing pipeline decides
whether its required indicators are present. Existing freshness TTLs, penalties,
agent dependencies and confidence floors are unchanged.

The retained UTC-midnight representation is an observation-date convention,
not an intraday publication or receipt time. FRED distinguishes observation dates
from real-time/vintage periods. This adapter is not authenticated point-in-time
training ingestion; it does not establish when a revision was first known.
An old series can therefore correctly leave the basket stale. Do not increase a
TTL or replace missing series merely to obtain a confident opinion.

Official API semantics:
https://fred.stlouisfed.org/docs/api/fred/series_observations.html
https://fred.stlouisfed.org/docs/api/fred/realtime_period.html

The existing series map and requested aliases are retained, not newly qualified.
Their units, proxy semantics, update frequency, licensing and current availability
must be reviewed independently. No numeric value, fee or macro series was invented.

## Five-area closure cross-check

The following is a repository checkpoint, not an AWS acceptance certificate.
PR #148's authentication component is not full production identity or a completed
restore drill; its new audit database still needs coordinated backup coverage.

| Area | Existing component/workstream | Remaining evidence or implementation |
|---|---|---|
| AWS operation | Merged token renewal, history warmup, protection and backup tools | Exact running revision; today's real token and quotes; actual model-response mode; restart; received independent alerts; coordinated off-host restore |
| Institutional execution | #146 immediate daemon route and explicit historical recovery; #148 scoped authenticated bookkeeping recovery; #136/#137/#140 prerequisites | Default/environment deployment remains unchanged; scheduled multi-slice operation, authenticated configuration/recovery, position-linked capacity release and target-host qualification |
| Learning | #138 feedback/drift; #139 shadow; #143 fitter; #145 datasets; #147 receipt-timed ingestion | Real source capture/features/costs, scheduling and endpoints; cumulative trials; unused holdout/forward evidence; authenticated approval/activation/rollback; effectiveness |
| Intelligence sources | Existing real adapter factory; this conservative clock and configuration diagnostic | Actual AWS configuration plus successful fresh retrieval and proof usage; source meaning/vintage/availability qualification |
| Wider scope | #144 NSE expansion; existing contract/fee/margin/segment structures | Qualified actual fifty-name bundle and budgets; MCX pilot admission; futures/options/currency lifecycle, settlement and fees/margins with genuine data |

A module's existence or green tests do not close an operating requirement.
No current active owner is inferred for a subtask just because a neighbouring PR
mentions it. Keep explicit ownership acknowledgements in the existing PR threads.

The original Research publisher requirement also remains: genuine host dataset and
trial register, accepted report or precise refusal, and individual disposition of
all three sibling panels. Receipt ingestion does not publish those reports.

## Verification boundaries

Tests use recorded synthetic HTTP responses and no account/provider request.
Regression and deliberate-removal cases exercise the actual FRED adapter,
production provider factory, failover wrappers, diagnostic and command. Old data
cannot become fresh just because a different series has a recent observation.
No successful retrieval, model skill, price-source freshness or actual alert is
inferred from this local suite. Owner approval is required for merge/deployment.
