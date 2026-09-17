# Opt-in Kite daily history for the five-name paper pilot

## Scope and status

The daemon and operator preflight share an explicit Yahoo / Kite / none selector.
Yahoo remains the default. Choosing Kite creates a read-only history client without
making a request at construction. Missing credentials or an unknown source refuse;
there is no automatic source fallback, token renewal or live-order route.

This is the continued Item 2 implementation in existing PR #133. Owner-main token
renewal (#129 at 5bacb52337351c41fe782ac848edf1ae33647614) is integrated into the review
branch, not merged into main by this work. The earlier 51 portfolio-risk mutations
and seven missing-history paper-exit tests remain separate from the new adapter tests.
The watchlist remains five names, and no cap, statutory rate or margin number changes.

## Reuse and corrections

The reviewed candidate reuses peer KiteDailyFeed/KiteDailyHistoryProvider code, the
existing normalized instrument-master parser, Decimal Candle model and closed-session
DailyHistoryProvider behavior. A donor-file snapshot was retained before editing;
other peer worktrees were not modified. The shared selector is used by both daemon
and preflight, and runtime fingerprints now include the chosen history source.

Seven independent boundary cases initially yielded five failures in four areas:
401/403 rejection left another cached symbol usable; request length rounded down to
whole days; an unexpected transport exception could leak its diagnostic into logs;
and evidence snapshots waited behind provider I/O. The corrected reader revokes all
cached observations and evidence after rejected authentication, compares the exact
timedelta, redacts unexpected transport errors and uses a separate short-held state
lock. Cached protection reads and evidence snapshots never take the network lock.
The session stays rejected until a new reader is constructed after legitimate login.

## Data and request contract

The transport permits only GET to the fixed api.kite.trade HTTPS origin, restricted
to the NSE instrument master and daily historical-candle paths. Redirects are refused;
ordinary certificate/hostname verification is retained. The default client makes one
attempt per request, at one request per second, with a five-second timeout. HTTP rate
limits use the existing circuit breaker; they are not bypassed with other identities.

The reader derives numeric tokens from one unambiguous current NSE cash master row,
never from hand-entered documentation examples. Missing/duplicate identities, invalid
lot/tick metadata, unsupported scope and conflicting explicit units refuse. The
master's display classification is not treated as independent ETF identification;
the operator's scoped cash instrument identity is preserved. MCX is not admitted.

Full timestamp/open/high/low/close/volume records are validated as a complete series.
JSON money is Decimal, not binary float. Nonfinite values, boolean/text numbers,
contradictory ranges, malformed/duplicate/out-of-order sessions, unbounded payloads
and fractional/negative volume refuse. Only closed sessions enter the daily cache.
No missing price, session, volume, corporate-action adjustment or margin is invented.
The existing sample/coverage and freshness thresholds remain unchanged.

Application resource bounds are 10,000,000 response/decompressed bytes, 250,000 master
rows, 400 candles and a request span of 366 days. These are resource budgets, not
claims about broker account entitlements, exchange rules or allowable trading limits.
Daily history retains the existing UTC-day cache boundary; master refresh uses IST.
Exact instrument identity and observation time bind cache entries. Cache inspection
performs no provider request, and future observations are not available to earlier clocks.

## Owner-run preflight, after review

Run from the repository in an owner-managed environment which already contains
ZERODHA_API_KEY and ZERODHA_ACCESS_TOKEN. Do not paste them into commands, logs or PRs.
The selector reads those two existing environment values only, never credential files.
Keep TRADING_LIVE_MONEY_ACTIVE=false. Interactive login/2FA remains an owner step.

    python scripts/check_pilot_risk_gates.py --online --history-provider kite --output /private/operator-selected/new-report.json

Without explicit --online, no provider is constructed. Use --directives and
--sector-map for the actual intended host input files rather than assuming the
committed examples match a deployment. A new output path is required; existing reports
are not overwritten. The previously verified private atomic report-publication logic
and exact input-file digests are retained. The environment selector is
PRAMANA_DAILY_HISTORY_PROVIDER=kite; changing it is an operator action, not performed here.
PRAMANA_BOOK_RISK_HISTORY=daily and the required sector map remain necessary for Item 2.

Exit 0 means this preflight found usable records and completed requested publication,
not that the live host is accepted. Exit 1 means dataReady=false, with a diagnostic
report. Exit 2 means input/publication prerequisites refused. The report includes
source/master/response hashes, actual row counts and last sessions, without secret
values. These hashes provide traceability, not a remote attestation or independent
qualification of the vendor's corporate-action adjustment policy.

## External qualification still required

On 17 September 2026 at 13:39:17 UTC, the development Mac's existing private session
passed file-privacy checks but was locally expired. No history/API request was made,
no session or credential file was changed, and no new login was attempted. This is
not evidence about the Lightsail host's session. A fresh owner login and permitted
read-only retrieval are still needed before claiming real records are available.

Provider price basis is explicitly marked
provider_supplied_adjustment_basis_not_independently_verified. An authenticated
response, current master and sufficient aligned closed sessions must be retained and
reviewed separately from synthetic tests. Actual-host loaded settings, exits, alerts,
restore and operator acceptance remain open. No deployment, service restart, account
migration or live-money activation is performed by this implementation.

## Verification records

The reviewed donor plus surrounding tests passed 155 cases before the additional
boundary checks; the four defect areas were reproduced as five failed/two passed
cases. The corrected direct adapter tests and expanded boundary suite pass. Exact
final counts are recorded in docs/evidence/kite-history-implementation.json.

The adapter adds an offline 57-case guard-removal suite. Each control must pass, its
mutated copy must fail a named assertion with no collection error or skip, and the
copy must be restored with original-source hashes unchanged. Child environments omit
credentials and deny Python network-connect/DNS/send events. This is controlled test
isolation, not a general OS security sandbox. The first campaign caught 53/57; four
later-validation-masked tests were replaced with explicit boundary-specific cases,
without weakening source or assertions. The repeated campaign passed all 57.

The full guard-to-test map is in docs/evidence/kite-history-guard-inventory.json and
is listed in the PR. Local verification does not imply the new published-head CI is
complete; the PR records that run's actual status separately.

## Primary API references

API shape and limits were checked on 17 September 2026. These references describe the
API, not this account's current entitlement, data authenticity or adjustment basis.

- https://kite.trade/docs/connect/v3/historical/
- https://kite.trade/docs/connect/v3/market-quotes/
- https://kite.trade/docs/connect/v3/exceptions/
