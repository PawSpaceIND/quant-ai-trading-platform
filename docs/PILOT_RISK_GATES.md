# Item 2: required five-name pilot book-risk controls

## Status and authority

Source changes for `feat/arm-risk-gates`, based on main
`1c78ad47c0fb26c32271804df41a782877cc09bf`. Later main `9bf0c29...` changes five
specialist/pipeline files, with no filename overlap. The local full-suite evidence
below belongs to the tested base; any GitHub merge-checkout result is separate.

This work is DRAFT. Further risk-test editing was tool-blocked. The required
systematic guard-removal mutation campaign has NOT been completed. The two real
public-history attempts returned no usable records. Do not call Item 2 closed,
merge it automatically, deploy it, or widen the watchlist on this evidence.

No real broker request, paid LLM call, account credential read, production ledger
write, deployment, service restart or live-money change was performed. All financial
risk calculations retain Decimal and the existing policy thresholds.

## What was missing

The Python runtime already knew `PRAMANA_BOOK_RISK_HISTORY=daily` and sector maps,
but the deployment Compose service did not pass either setting into the daemon.
Its telemetry also equated a history-provider object with an armed control without
reporting sample availability. Unmapped holdings were outside sector aggregation.

## Wiring and runtime behavior

The ghost service now enables `PRAMANA_REQUIRE_BOOK_RISK_GATES=true`, defaults
`PRAMANA_BOOK_RISK_HISTORY` to `daily`, passes the optional `PRAMANA_SECTOR_MAP_JSON`,
and mounts the committed five-name map read-only at `/app/pilot-sector-map.json`.
The host-file knob is `PRAMANA_SECTOR_MAP_HOST_FILE`. Missing bind sources refuse
rather than Docker creating an empty directory. Market-monitor's environment is
unchanged. `TRADING_LIVE_MONEY_ACTIVE=false` remains fixed.

The existing precedence is retained: a nonempty directives `sector_map` wins over
the environment; inline JSON wins over the mounted file. Every symbol in the required
watchlist must have a group, regardless of which source wins. Duplicate JSON keys,
normalized duplicate symbols and non-string records refuse instead of turning null
or numeric data into a plausible group.

In required mode, missing sector coverage or history wiring refuses startup before
stream assembly. The entry firewall checks again after startup: losing inputs cannot
fall back to an unarmed approval. Every positive projected holding, including a
holding not in the watchlist, must be mapped. Covered risk-reducing exits retain the
existing Warden exemption before these entry checks. No exposure cap is raised.

The shared daily-history provider still requests at most once per symbol per UTC
date and is reused by regime context and book risk. Telemetry reads the cache only;
it must never make a network request while protection holds the broker lock.
The required adapter rejects naive/future/unclosed/mismatched/duplicate session bars
and history older than seven calendar days. Seven days is this patch's explicit
engineering freshness budget, NOT an exchange calendar rule or a sourced market
statistic. It is fingerprinted for owner review. Legacy optional builders retain
an unset freshness budget and do not become required implicitly.

## Proposed operator risk groups, not official industry classifications

`deploy/pilot-sector-map.example.json` covers ONLY the existing five-name watchlist:

| Symbols | Proposed operator group | Basis |
| --- | --- | --- |
| INFY, TCS | IT_SERVICES | The companies' own technology/IT services descriptions |
| RELIANCE | DIVERSIFIED_BUSINESSES | Its disclosed energy, petrochemicals, retail, digital and other businesses |
| GOLDBEES, SILVERBEES | PRECIOUS_METALS | The funds' gold and silver investment objectives |

These are proposed concentration buckets, not NSE/GICS codes, measured correlations,
or investment recommendations. Combining the two metal ETFs is an operator policy
choice: separate tickers must not automatically receive separate concentration
budgets. Reliance is not assigned fabricated segment weights. A 50-name expansion
will need a reviewed, complete map for those names; that is not included here.

Primary source descriptions checked on 17 September 2026:
- Infosys: https://www.infosys.com/about.html
- TCS: https://www.tcs.com/who-we-are
- Reliance: https://www.ril.com/businesses
- Gold ETF: https://mf.nipponindiaim.com/FundsAndPerformance/Pages/NipponIndia-ETF-Gold-BeES.aspx
- Silver ETF: https://mf.nipponindiaim.com/FundsAndPerformance/Pages/NipponIndia-Silver-ETF.aspx

## Interpreting the persisted riskGates payload

`armed` means the configured control is wired; it is NOT proof of usable history.
History gates additionally publish `dataReady`, `records`, `coveredSymbols`,
`alignedIntervals`, `source` and an explicit reason. A provider with an empty cache
is armed but dataReady=false. The sector gate reports required scope coverage.
Full mapping, required scope and freshness policy enter the runtime fingerprint,
so a changed grouping cannot retain the same strategy identity.

The existing floors remain 60 return intervals for covariance and 100 for expected
shortfall. Sample readiness is not approval of the proposed allocation: every entry
still calculates the exact projected book. Existing thresholds remain 0.25 sector,
0.45 correlation-adjusted gross and 0.03 historical expected-shortfall fractions.
These are pre-existing risk policies, not statutory rates or guarantees of loss.

The adapter checks record identity, ordering/alignment, age and sample length; it
does not certify exchange-calendar completeness, corporate-action adjustment quality,
provider licensing, historical point-in-time revisions or future strategy performance.
If every series misses the same real session, cross-series alignment alone cannot
identify that absence. Those data-qualification limits remain explicit.

## Owner preflight and deployment instructions

Do not deploy from this draft branch. After review, completed mutation certification
and an owner merge, preserve the deployment's existing root `.env` and directives.
The root `.env` may set `PRAMANA_BOOK_RISK_HISTORY=daily` and an absolute
`PRAMANA_SECTOR_MAP_HOST_FILE` pointing to the reviewed map copy. The container path
is fixed; setting a host path in `PRAMANA_SECTOR_MAP_FILE` does not change the bind.
Review an existing directives `sector_map` because it takes precedence.

Before any deployment, the owner can run a public-data-only check in the reviewed
checkout (no ledger, broker or model needed):

```bash
python scripts/check_pilot_risk_gates.py --online --output /tmp/pilot-risk-preflight.json
```

Use a new output filename: it refuses to overwrite an existing file. The output
contains dates, counts and a source-row digest, not raw prices or credentials.
Without --online, no network call is made. A nonzero exit or dataReady=false is not
permission to proceed. This process's allArmed flag describes its own constructed
checks, not the running Lightsail host; hostAcceptance remains false.

Use the normal owner-operated `./scripts/deploy_pilot_host.sh` only after approval,
on clean main and in its safe window. Deployment is not performed here. Then inspect
the actual persisted host riskGates: all three required gates must be armed, the map
must cover the exact watchlist, and both history gates must show sufficient records
and dataReady=true. Startup and heartbeat existence alone are not acceptance.

## Executed evidence and unresolved gates

Fresh Mac baseline: 1536 passed, 13 failed, 2 warnings, 14 subtests passed.
Candidate: 1575 passed, the identical 13 failures, 2 warnings, 14 subtests passed.
The 13 are pre-existing deployment-script portability failures, compared by exact
JUnit identity. No tests were skipped or assertions weakened. The focused suite is
100 passed, including 39 new synthetic cases. All 453 selected source/test/script/
config hashes are unchanged during full certification. Ruff and whitespace checks
pass using the absolute existing project venv; `/usr/local/bin/ruff` is absent.

The new executable cases inject missing mappings/history before boot and after boot,
insufficient samples, stale/future/naive/duplicate/wrong-instrument/unclosed bars,
malformed settings and unmapped holdings. Concentrated, correlated and heavy-tail
books are refused by the real firewall; covered exits still pass. The environment
builder and persisted runtime payload are exercised, not just a configuration file.

These fault-injection tests are NOT represented as a completed guard-removal
campaign. No systematic source-guard sabotage table can be claimed yet. Further
risk-test edits were blocked by a tool safety check; those edits were not retried.
Required mutation certification remains a review blocker.

Real public-data attempts at 05:58:16Z and 05:59:28Z on 17 September 2026 failed.
The first hit a Python certificate-store error. The second used the already-installed
certifi CA bundle for that subprocess, retaining certificate and hostname validation;
Yahoo returned provider_rate_limited and the existing circuit breaker opened.
Both reports correctly contain dataReady=false and zero records. No provider was
substituted, TLS verification disabled, or missing bar fabricated. This failure does
not demonstrate that Lightsail has the same network issue, nor that it is healthy.

Exact results: `docs/evidence/pilot-risk-gates-checkpoint.json`. Real records,
required mutation certification, exact published-head CI and target-host acceptance
must all remain separate gates. No watchlist cap, watchlist entry, instrument token,
fee schedule, margin number or MCX admission rule changes in this item.
