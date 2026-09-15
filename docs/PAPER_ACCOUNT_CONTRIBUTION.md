# Recorded paper-account contribution

The private Portfolio page explains recorded account P&L by instrument, including closed positions. It uses the actual paper broker's fill and cost ledger together with the matching engine valuation. This is distinct from the independent-case research lab and continuous research simulator. No real brokerage execution or strategy qualification is established by this view.

## Accounting and cost convention

The paper broker keeps cash fees out of average entry cost and expenses them immediately. The reader reconstructs weighted average entry from buys and gross realized P&L from partial/full sales, then applies every recorded cash-debit charge to its instrument. Consequently, fees on an open buy appear in realized-after-fees P&L even before a sale. This preserves the existing tracker and dashboard convention.

`gross realized P&L − all recorded cash fees + current unrealized P&L = net contribution`

Per-instrument contribution is net P&L divided by starting account capital, expressed in percentage points. Summed contributions reconcile to the engine equity change from inception. This assumes the current paper account's cash-flow model: no deposits, withdrawals, dividends, corporate actions, interest or FX. It is not a security return, time-weighted return with external flows, benchmark effect or tax calculation.

Recorded SPREAD and SLIPPAGE rows are noncash model drag already included in fill prices. Their add-back plus cash fees yields P&L before modeled costs on the same fills and final marks. These estimates are not exchange-observed execution costs, midpoint fills or an achievable alternative portfolio. Model, infrastructure and data bills remain excluded. Cash reconciliation proves consistency with recorded charges; it cannot prove that all real-world charges were captured or that the historical fee schedule was correct.

## One read snapshot and explicit failure states

`readPortfolioSnapshot()` opens one read-only SQLite transaction for engine valuation, ledger head, account, positions, fills and costs. A concurrent WAL writer can commit, but its later fill cannot enter that already established read snapshot. The next read detects that the old valuation no longer matches the ledger. No database is repaired or created by this reader.

The reader independently checks scope/tenant, fill quantity/notional/side/chronology, uncovered sales, duplicate identities, orphan/duplicate/negative/nonfinite costs, cash-debit flags and times, persisted and valued position coverage/average cost, per-holding arithmetic, aggregate P&L and equity. Only INR/INDIA cash EQUITY and ETF records qualify. Amounts use JS numbers with consistency tolerance `1e-8 + 32 × Number.EPSILON × max(1, |a|, |b|)`; they are display/analysis checks, not a new settlement ledger. Original broker accounting remains Decimal. UI amounts round to two decimals and the private report retains the numeric residual.

- **Available:** recorded accounting matches, final marks are current and spread/slippage records are present.
- **Incomplete:** accounting matches, but a stale/missing holding mark or missing/unknown noncash cost record withholds affected values. No mark is carried forward as current contribution. Closed-position results and known charges remain visible.
- **Outdated:** the valuation's ledger ID differs from the current head. No joined contribution is produced.
- **Invalid:** inconsistent or unsupported records withhold the report.
- **Unavailable:** missing stores, unsupported valuation mode or bounded-history limits cannot supply a report. Legacy ledger-fill and historical replay valuations do not establish current account contribution.

Invalid/outdated evidence, and missing required contribution storage for an engine-live valuation, suppress Portfolio/Overview totals and cause the existing current-valuation readiness check to fail. Numeric zero placeholders in the unavailable portfolio response are not account results; consumers must check status. The private UI shows a withholding explanation and omits those tiles/holdings. A matching but aged snapshot retains known historical accounting while current contribution totals remain null. Snapshot freshness is 30 seconds (with the existing 5-second future-clock tolerance); a qualifying holding tick must match the valuation and be no more than 120 seconds old at that valuation.

Missing SPREAD/SLIPPAGE records are not assumed to be zero. Unknown noncash cost codes withhold that instrument's modeled cost bridge and flag aggregate coverage. The bounded reader allows 20,000 fills, 200,000 cost rows and 1,000 instruments; overflow returns an explicit unavailable state without omitting economic history. Larger-scale archival/incremental accounting remains future work.

## Dashboard, export and Atlas

The Portfolio panel provides summary totals, search by symbol/asset class, impact/fee/symbol ordering, 25-row pages, mark state and recorded-cost coverage. Filtering does not change account totals; that distinction is stated beside the table. Closed positions remain included.

`GET /api/portfolio/contribution` uses the existing private session proxy and returns a no-store JSON attachment. It exports complete/incomplete state, not a caller-selected file. Invalid/outdated evidence returns 503; unavailable evidence returns 404. Workspace remains usable when a contribution mismatch is detected. The source hash covers the read account, fills, charges, positions and evaluated portfolio snapshot; it is a local consistency identifier, not an independent signature. Exports from different evaluation times may have different generation metadata or freshness state.

Atlas receives all totals, coverage and limitations with at most 50 instrument rows. Missing-mark rows come first, followed by absolute net contribution. Omitted-row and unavailable-row counts are explicit, and totals still cover all rows. Raw fill/cost arrays are not sent through this contribution context. The handoff explains the immediate-fee convention and does not auto-submit. The existing company-cutoff mode excludes this current-account context. Source cloud publication and Worker ingestion both remove `paperContribution`; it is not presented as a current interactive report on the hosted snapshot viewer.

## Local verification — 14 September 2026

The shared fixture uses the real `PaperBrokerService`, `PortfolioTracker`, internal reconciliation and `PilotTelemetry` with synthetic prices/fees. Python reproduces it and Node reads it. Six fills scale into TCS, sell half, close a losing INFY position and leave an ETF open:

| Measure | INR |
|---|---:|
| Starting capital | 10,000 |
| Reconstructed cash | 9,644.39626 |
| Gross realized P&L | 50.1 |
| All cash fees | 1.30374 |
| Realized after fees | 48.79626 |
| Unrealized P&L | 49.6 |
| Net contribution | 98.39626 |
| Equity | 10,098.39626 |
| Recorded spread / slippage | 13 / 1.3 |
| P&L before modeled costs | 114 |

TCS retains a 107.84 average entry and three shares; its net contribution is 112.63725. Closed INFY contributes −12.28011. The open ETF contributes −1.96088, including 0.08088 of already expensed buy fees. These independent arithmetic expectations are checked against the producer and reader.

Regression cases cover stale/partly fresh valuations, missing drag, unknown noncash costs, contradictory account/position/cost/tenant/scope data, unavailable storage, malformed holding payloads, private export and persisted mocked Atlas context. A real second SQLite WAL connection commits a fill between the valuation and account reads: the first result remains coherent at ledger 6 and the next read withholds the outdated valuation. Publisher/Worker tests verify exclusion of the new private report.

The populated browser/API fixture has 55 instruments, 110 synthetic fills and 330 cost rows. Its 52 additional closed round trips lose 0.24 each, producing net contribution 85.91626, 2.34374 recorded cash fees, 23.4 modeled spread and 2.34 modeled slippage; the modeled add-back remains 114. Search, clear/empty results, fee/symbol ordering and 25/25/5 pagination are verified. Desktop width is 1280px; mobile document width is 390px, with a 324px table region and 154px search/sort controls. The invalid-account view contains no numeric metric grid.

Full verification passes **517 Python**, **52 UI** and **13 Worker** tests, CI-scope Ruff plus the changed publisher, TypeScript and webpack production build. Two existing Starlette warnings remain. API evidence verifies private export 401 without a session, matching report rows/totals/source hash and no-store attachment when authenticated, incomplete exports with null current totals, and invalid/outdated 503 exports while workspace stays 200 and the marks gate fails. No real broker order, market-source capture, provider request, deployment or signed acceptance is part of this synthetic drill. Exact revision, final browser/Atlas evidence and CI are retained separately with the closure artifacts.

The final compiled browser run observed a successful contribution download and no console warnings/errors. Atlas conversation `eab48f19-3873-4110-a7a4-2098e730a134` persisted the partial-mark report: 50 rows, five explicitly omitted rows, one unavailable row placed first, and all-account totals with current net P&L null. An intentionally empty provider key produced the expected visible configuration error and zero provider calls. The temporary browser tab and isolated server were stopped after verification.

The first hosted CI build exposed a deployment-copy issue: private tests still imported API routes deliberately removed from the static viewer. The cloud packaging script now omits the test directory from that copy, while the separate full UI CI job retains test type-checking and execution. The local static export passes with this packaging correction; no runtime authentication or test source was removed.

## Benchmark boundary

IBKR documents integrated holdings, performance/activity reporting and benchmark attribution. This increment advances the account-consistency and instrument/cost contribution part of that comparison. [IBKR PortfolioAnalyst features](https://www.interactivebrokers.com/en/portfolioanalyst/features.php).

Benchmark/sector/factor attribution, external-flow performance, corporate actions/income, FX/multi-asset accounting, observed broker execution analysis and external reconciliation remain open. Real input quality, target-host operation and strategy-specific forward performance are still required before pilot closure. This supersedes the earlier *basic recorded-account contribution* gap, not the broader vendor-parity objective.
