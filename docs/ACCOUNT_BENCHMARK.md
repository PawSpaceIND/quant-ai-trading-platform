# Recorded account versus benchmark

The private Portfolio screen compares the recorded paper account with NIFTY 50 or NIFTY BANK over up to 20, 60 or 252 completed-session intervals. This closes the initial account-to-benchmark display and calculation workflow. It does not establish live returns, strategy effectiveness, full performance attribution or release acceptance.

## Account and price contract

`readPortfolioSnapshot` holds one SQLite read transaction for engine valuation, account, positions, fills, costs and comparison. The existing paper-contribution validator must first reconcile the full account. Invalid/outdated/missing accounting withholds the comparison. Stale current marks do not invalidate a separately dated historical book; the report states the account observation cutoff and never extends beyond it.

Reconstruction starts with recorded initial capital and applies every recorded fill and cash debit in order. A buy spends its actual paper notional; a sale receives its notional; cash fees are expensed on the fill date. Spread/slippage already affect fill prices and are not deducted twice. Closed positions retain their cash gains/losses. The first eligible baseline is the declared session before the first historical fill. An empty ledger or absent prior session cannot create an invented history.

Each reported date values that day's actual ending quantities using matching symbol/market/asset-class daily closes. This is historical marking of the recorded book, separate from the Risk lab's fixed-current-holdings repricing and from the engine's intraday forward-observation samples. Cash stays in equity. The two benchmark identities are INDIA/INDEX on NSE in INR with explicit provider IDs. Dates, positive finite closes, unique identities, source capture, calendar bounds and the documented special session are validated through the shared daily-history contract.

A missing held-instrument close makes that day's account equity null. Missing benchmark closes remain null for that benchmark. A closed instrument needs no price after its quantity reaches zero. No exposure is omitted, no missing close is forward-filled and no session is skipped to manufacture a return. Historical fills outside the declared regular cash session fail validation; fills after the last completed report session are counted as excluded. Account history before calendar coverage is unavailable.

The report is bounded to 253 marks, at most 40 simultaneous holdings per reported date, 20,000 fills and 200,000 cost rows. Earlier fills and fees still establish the book entering a truncated display window. The full source calendar is bounded to 1,000 sessions, with at most 500 supplied instrument histories and 1,000 observations per instrument. Exceeding a bound fails rather than truncating economic exposure.

## Calculation convention

For each selected range, both curves rebase to 100 at the first date when its baseline exists. A missing baseline withholds that entire curve; an interior missing value creates a visible gap. Aggregate returns, drawdowns and relative-risk statistics require the whole selected account/benchmark range to be covered.

With account growth `A = equity_last / equity_first` and benchmark growth `B = close_last / close_first`:

- Account return: `A - 1`, after recorded cash fees and fill-price friction.
- Benchmark return: `B - 1`, price return without dividends or execution costs.
- Excess return: `(A - 1) - (B - 1)`, expressed as percentage points.
- Relative wealth return: `A / B - 1`, expressed as a percentage.
- Maximum daily drawdown: maximum `1 - value / prior_running_peak` in the selected daily series.

At least 20 complete adjacent-session intervals are required to display relative-risk statistics. For daily account return `a`, benchmark return `b` and active return `d = a - b`, sample covariance/variance uses the `n - 1` denominator:

- Annualized tracking error: `stdev(d) * sqrt(252)`.
- Annualized information ratio: `mean(d) / stdev(d) * sqrt(252)`.
- Beta: `cov(a, b) / var(b)`.
- Correlation: `cov(a, b) / (stdev(a) * stdev(b))`.

Constant series leave denominator-dependent statistics undefined. Nonfinite results are never emitted as valid numbers. The display floor is not proof of statistical reliability. Daily drawdown excludes intraday extremes, and there is no alpha, projected return or guaranteed-loss claim.

## UI, export, Atlas and privacy

Benchmark/window selectors update the chart, metrics and private export link. The daily table supports pagination and selecting a session to inspect its actual holdings, fees and missing closes. Missing evidence stays visible and withheld statistics are labelled.

`GET /api/portfolio/benchmark?benchmark=NIFTY%20BANK&lookback=20` returns the source report and selected calculation as a no-store attachment. Defaults are NIFTY 50 and 60; unsupported/duplicate selections return 400, missing source 422 and invalid accounting/history 503. Authentication applies before any private API response. The workspace remains usable when this report is unavailable.

The Atlas button prefills the selected benchmark/window. A submitted request stores all six bounded comparison summaries, source/account hashes, coverage dates and explicit omitted-row counts. It excludes raw fills, costs, holdings-by-day, curves and input history. It remains read-only. Company-cutoff requests retain their separate historical context rules. The cloud publisher and Worker strip this private report; it is not added to the hosted read-only snapshot.

Retain the original ledger/valuation and market history with the selected recovery inventory when reproducing a report. Its local hashes identify supplied inputs but do not authenticate provider data or detect coordinated rewriting. An export alone does not archive the underlying records.

## Verification and remaining qualification

The shared fixture uses the real Python paper broker, friction accounting and telemetry for six dated fills across 31 marks, including scaling, a partial sale and an already closed losing instrument. An independent Decimal oracle checks daily cash, equity and fees. Node tests also cover missing historical/benchmark closes, invalid mappings/timestamps, off-session fills, stale sources, account cutoff, the 253-mark carry-in, constant/short samples and a real concurrent WAL writer.

Local validation passes 652 Python, 72 dashboard and 13 Worker tests, required/changed-script Ruff, TypeScript, private production build and hosted static export. The isolated production-server smoke verifies all 31 daily oracle values, six benchmark/window exports, unauthenticated rejection and saved Atlas context without a model call. Desktop/mobile browser checks cover benchmark/window changes, export URL, pagination, historical closed holdings, Atlas submission, missing-price details and an actual two-segment account chart gap. Mobile document/panel widths are 390/360px and no warnings/errors were captured. Temporary server resources are stopped. The Linux container verifier runs the same smoke against a separate synthetic account and the actual dashboard image; its revision-specific CI artifact establishes the image result.

The data remain provider-close adjustments unverified. NSE price indices are nontradable and exclude dividends, so the comparison is not a like-for-like investable net-return benchmark. External flows, income, splits/corporate actions, financing, FX, derivatives, sector allocation/selection and factor attribution remain unsupported. No source or strategy gate is satisfied by this historical reconstruction.

Bloomberg PORT documents integrated positions/performance/risk, attribution, multi-asset models and scenario analysis. This addition advances our basic benchmark-relative reporting; it is only part of that capability set. [Bloomberg's documented PORT capabilities](https://professional.bloomberg.com/products/bloomberg-terminal/portfolio-analytics/). Full broker lifecycle, qualified sources, target-host operations and broader benchmark parity remain open in [the closure register](PILOT_CLOSURE.md).
