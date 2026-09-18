# Item 3 — source-bound NSE fifty-name preparation (PR #144)

## Status and rollout boundary

Implementation is for offline preparation and fail-closed expanded-pilot mapping.
The official constituent CSV has **not** been obtained in this development run.
Consequently no real fifty-name selection, source-qualified example, current fifty-name
mapping bundle, or fifty-name host acceptance is claimed. Keep this PR draft.

The existing five-name directives and Compose defaults remain unchanged. No risk,
position, watchlist, health or AI-budget cap is raised. The owner alone merges and
deploys. No provider credentials are read by the generator; it makes no network calls.

Starting point: owner-merged `bf3812088e4308eb5372203669d9e07cd8e74562`.
The owner's AWS screenshot at 2026-09-17T19:09:01Z reported the real daemon's three
required gates armed/data-ready, with 600 records / 119 aligned intervals and five
names covered. This is screenshot evidence, not independently downloaded raw telemetry.
It permits beginning Item 3, not declaring a fifty-name rollout complete.

## Implemented preparation

`scripts/prepare_nse_watchlist.py` reads an operator-supplied Nifty constituent CSV,
a current Kite NSE instrument-master CSV, the actual five-name founder directives,
and explicit current budget limits. Its pure helper lives in
`quant_ai.governance.nse_watchlist`.

Selection is reproducible: keep INFY, TCS, RELIANCE and both existing metal ETFs.
Take 48 of the supplied 50 cash-equity constituents, removing two non-retained
names from the most represented industry (ties: industry, then symbol). Never
remove a group's last member. Preserve all non-watchlist/non-sector directive
fields, including capital, position limit, risk mode and founder instructions.

The selected universe must have at least five groups, none with more than twelve
names. Those are explicit **proposal preparation policies**, not exchange rules or
a replacement for the existing 25% marked-exposure sector firewall. The publisher's
`Industry` strings supply equity groups; the two retained ETFs preserve the reviewed
`PRECIOUS_METALS` grouping. Review industry granularity before rollout. No industry,
liquidity rank or security classification is inferred from a ticker name.

Index membership is only a large-cap/liquidity proxy. It does not measure current
spreads, turnover, executable depth or investment quality. The source CSV's retrieval
time does not establish its effective membership date. Upcoming index changes must
not silently become current memberships. Source identity/date and reuse rights need
operator qualification; hashes prove bytes, not source authenticity or entitlement.

The master reader reuses `marketdata.instrument_master.normalize_row` and validates
exact selected NSE/cash identities, complete one-to-one token mappings, positive
finite Decimal ticks, a cash lot of one, no derivative expiry/strike, and no duplicate
symbol/token aliases. It refuses missing, malformed, oversized or ambiguous inputs.
The supplied constituent observation must be no more than seven days old; the master
observation no more than 24 hours old. Both must be timezone-aware and not future-dated.
These are local preparation freshness policies, not assertions about vendor publication.

For a paper pilot expanded beyond five instruments, `build_ghost_runner` checks
subscription bijection and the existing cash/50-name cap before opening the persistent
broker. Existing five-name and non-pilot paths are unchanged. This boot check validates
mapping structure, not authenticity of a manually edited instrument token. The source-
bound generator and later real-feed verification remain necessary.

## Operator inputs and example invocation

Use the actual current base directives, not an unrelated example with different
capital or instructions. `deploy/nse-watchlist-selection.example.json` describes the
recipe and pending inputs; it deliberately is **not** a made-up fifty-name list.

```bash
python3 scripts/prepare_nse_watchlist.py \
  --base-directives /private/current-five-name-directives.json \
  --constituents /private/ind_nifty50list.csv \
  --constituents-observed-at '<actual-aware-retrieval-time>' \
  --master /private/kite-nse.csv \
  --master-observed-at '<actual-aware-retrieval-time>' \
  --daily-call-limit '<current-operator-limit>' \
  --daily-token-limit '<current-operator-limit>' \
  --output-directory /private/new-fifty-name-proposal
```

The directory must not exist. It is created 0700 with 0600 files: `directives.json`,
`sector-map.json`, `mapping.env` and `proposal.json`. The final report is written last;
a missing report means incomplete preparation, never an accepted partial bundle.
Stdout contains only the status and workload summary, not tokens or input contents.
The token IDs in `mapping.env` are market instrument identifiers, **not** access tokens.
No real access token, .env modification, model call, daemon start or deployment occurs.

After source/configuration review, the owner must deliberately select the generated
files using `PRAMANA_DIRECTIVES_HOST_FILE` and `PRAMANA_SECTOR_MAP_HOST_FILE`, and merge
the two generated mapping assignments into the private current root .env without
altering any credentials, financial limits, budgets or paper mode. Do not set
`PRAMANA_FOUNDER_DIRECTIVES_FILE` as a host-file workaround: Compose fixes its container
path. Keep Kite/daily history and required-risk settings enabled. A directives-inline
`sector_map` and the mounted sector map must agree; this proposal generates both alike.

The generated files are private by default. Before bind mounting, use owner-approved
file/group permissions readable by container uid/gid 10001 (for example group-readable
0640 with that group), not a world-readable credential file. Retain current backups,
Compose project, named data volume and ledger. Do not deploy from this development
branch or a test worktree. Do not use --force, clock overrides, or down -v.

## Workload and cost impact — no automatic budget change

The repository calendar and UTC-aligned ten-minute cadence yield 37 regular NSE
analysis opportunities on 18 September 2026: 09:20 through 15:20 IST. With one logical
consensus invocation per name/cadence, 5 -> 50 names changes the potential full-day
consensus workload from **185 to 1,850**, a factor of ten. This is an opportunity count,
not a measurement of actual calls, completed analyses, order fills or paid retries.
Slow sequential calls, stale prices, or other gates can reduce the realized throughput.

The existing code defaults to 500 calls and 2,000,000 tokens **per scope per UTC day**.
`consensus` and `headline_sentiment` have separate counters, not one shared 500-call
allowance. Headline demand depends on the available items and scoring cache. At an
unchanged 500-call consensus limit, at most ten full fifty-name sweeps fit into the
call count, versus 37 possible sweeps; token consumption can stop them earlier.
The current builder does not change these limits or silently alter scheduling to fit.

To cover every possible logical consensus invocation would require at least 1,850
consensus call slots on such a session, subject to measured latency and other controls.
That is a workload requirement, **not approval to raise a limit**. The owner must choose
between a reviewed larger allowance and explicitly accepting reduced AI coverage.
Changing evaluation prioritization/scheduling would be a separate reviewed change.

There is no defensible currency-spend quote yet: actual model, input/output/cache usage,
SDK retries and applicable prices have not been measured for fifty names. Token usage
is recorded after responses, so the existing token threshold is not an exact prepaid
currency ceiling. The report leaves token demand/currency cost null rather than
inventing a rupee amount or claiming that tenfold opportunities guarantee tenfold spend.

## Source evidence obtained and missing

Official source references:
- https://www.nseindia.com/static/products-services/indices-nifty50-index
- https://www.niftyindices.com/indices/equity/broad-based-indices/nifty--50
- https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv
- https://www.niftyindices.com/IndexConstituent/ind_nifty50list.csv
- https://kite.trade/docs/connect/v3/market-quotes/

The bounded public downloads and exact source snapshots remain in the GitHub evidence
artifacts. Run 35265190270 archived a successful Kite NSE CSV and an NSE timeout (curl
exit 28). Run 35265404949 also returned curl exit 28 for the publisher's linked CSV.
Those failures are not passing data qualification and have not been removed from history.
No credentials or access-control bypass were used. The recurring pull-request download
job is replaced with offline preparation verification to avoid repeating failed public
requests on every development push; standard CI/security/acceptance gates are unchanged.
This does **not** close the missing-constituent requirement or make this draft merge-ready.

## Verification and remaining closure

Synthetic fixtures create clearly named NSEFIX instruments; their invented identifiers
exist only as test inputs, never as deployment data. Guard-removal tests require a
passing control, a failing designated assertion after mutation, and restored copies.
They also exercise the unchanged pilot-51 refusal and actual concentrated-book firewall.
See the committed guard inventory and PR evidence for executed counts and failures.

Still needed: qualified official constituent CSV, owner-reviewed actual fifty-name
selection and generated committed example, current real mappings and source dates,
actual budget/latency/currency evidence, fifty-symbol history/feed qualification,
owner review/merge and rollout, and actual fifty-symbol running-gate acceptance.
The previously accepted five-name history is not fifty-name acceptance. Daily renewal,
received pre-open alerts, full off-host recovery, dashboard context and MCX remain
separate tasks. No MCX admission or live-order routing is introduced here.
