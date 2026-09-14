# Reconciled research portfolio contribution

The private Research page now explains inception-to-date simulated P&L by instrument, including fully closed positions. It separates execution-reference P&L, spread, adverse slippage and trading fees, and reconciles net contribution to the current simulated equity change. This is for the continuous research journal's long-only INR cash books. It does not add attribution to the actual running paper account or authorize a strategy.

## Accounting method

For each instrument, reconstruct every fill in order. Buys add fill notional and fees to position cost; sells remove average cost proportionally and realize proceeds less sale fees and removed cost. Remaining cost therefore includes the unallocated buy fees. Net P&L is cumulative signed fill cash flows, after fees, plus the current bid-marked holding value. Closed positions have zero remaining quantity/value but retain their cumulative result.

For the same executed quantities, calculate reference cash flows at each execution quote's midpoint. Keep terminal holdings marked at the original final bid. The exact bridge is:

`reference P&L − spread cost − adverse slippage − trading fees = net P&L`

Spread cost is quantity × half the bid/ask spread on each buy or sell. Slippage cost is quantity × the adverse difference between the simulated fill and the quoted ask for a buy or bid for a sell. Reference P&L is an accounting decomposition using the same fills and final marks, not an alternative executable strategy or a prediction of obtainable midpoint fills. It does not remove the bid marking of remaining inventory.

Instrument contribution is net instrument P&L / initial candidate cash. The sum equals the candidate net return because this simulator has no external cash flows, interest, dividends or corporate actions. The table expresses contributions in percentage points; these are not individual security returns. Realized plus unrealized P&L already includes fees: never subtract costs a second time.

If any held instrument has a stale/missing final mark, aggregate reference P&L, net P&L, return contribution and reconciliation difference remain null. Known costs, realized P&L and individually valued or closed instruments remain inspectable. No-event books have no observed result; observed cash-only books have zero P&L. Restoring valid marks restores current totals without erasing historical valuation gaps.

## Publication and validation

Use the existing [private portfolio publication command](PORTFOLIO_RESEARCH_WORKSPACE.md). New exports use `pramana.portfolio_workspace.v2`, add fill `quote_bid`/`quote_ask` and per-book `attribution`, and fingerprint the new attribution module. The raw simulator journal/report format is unchanged. Archive each published version with its evidence and implementation hash; retain it in the explicitly selected recovery inventory.

Python reconstructs cash, quantities, fee-inclusive average cost and P&L and refuses an inconsistent summary. The Node reader independently reconstructs that accounting and checks configured fee/slippage arithmetic, fill chronology, holdings/stale-state coverage, all contribution rows, totals and reconciliation. This detects inconsistencies even when an envelope hash was recomputed. Python uses Decimal with relative reconciliation tolerance 1e-24; the Node report consistency check uses a 1e-9 relative numeric tolerance (minimum scale 1). UI currency values round to two decimals; the private export retains decimal strings and the residual. These tolerances are not currency-settlement rules, and local hashes are not independent source authentication.

Valid v1 reports remain readable and now undergo the accounting checks, but their absent fill quotes cannot establish execution-reference attribution. The UI asks for a new publication. Unsupported fields or incomplete v2 attribution fail validation; no silent upgrade or invented quote is allowed.

The dashboard provides impact/cost/symbol sorting, 25-row pagination, a cash-candidate state and private export. Atlas receives the same validated contribution rows and totals, with explicit incomplete state and omitted fill/curve arrays; its handoff names the selected candidate and explains that costs are already in net P&L. The original 16-candidate, 50-symbol, 5,000-event and 4 MB publication limits still apply. Source cloud publication and the Worker continue excluding the entire research portfolio.

## Verified example and drill — 14 September 2026

The synthetic fresh book starts with INR 10,000. TCS scales into six shares through a partial fill and another buy, then sells three; INFY closes a losing position; BANK remains open. The independent result is:

| Measure | INR |
|---|---:|
| Execution-reference P&L | 50 |
| Spread | 14 |
| Adverse slippage | 3.035 |
| Trading fees | 1.214935 |
| Net P&L | 31.750065 |
| Latest equity | 10,031.750065 |
| TCS contribution | 48.716305 |
| Closed INFY contribution | -14.679035 |
| BANK contribution | -2.287205 |

The shared fixture regenerates in Python and is consumed in Node for empty, fresh, stale, partially restored, restored and reopened-position states. Tests reject altered cash, P&L, fees, position cost, fill quotes, fill chronology, oversells, missing rows, fabricated totals and mismatched schema even with recomputed hashes. Mocked-provider tests verify outbound and saved Atlas context.

Full local verification passes **516 Python**, **46 UI** and **13 Worker** tests, CI-scope Ruff, TypeScript and the webpack production build. The existing two Starlette warnings remain. Desktop 1280px and mobile 390px browser checks verify sorting, candidate switch, closed positions, stale totals, export/download and Atlas handoff. Mobile document width is 390px; table region and selector are 324px. No browser warnings/errors. The browser submission used an empty provider key, saved the exact incomplete attribution and returned the visible setup error; no provider call occurred.

API checks: unauthenticated export 401, authenticated matching attachment 200 with no-store, damaged report export 503 while workspace remains 200 and readiness unchanged. All six scenario reports validate. Missing account/event databases are not created. No broker order, real market capture, deployment or signed acceptance occurred. Temporary server and browser are stopped/closed after verification.

Fresh synthetic source evidence: `6ab4659ddfc992f9524c5087a56b12bc34613e6130408d9201ac8eade95b9d33`. Attribution/publisher/replay fingerprint: `bb3744e8c21a171f2afae788fa79316ea646efdd7cfc4ec1cd0b0bb13aaf898a` (Python 3.12.10). These are synthetic engineering evidence, not a performance track record.

## Benchmark boundary

Bloomberg PORT documents integrated positions, data validation, performance attribution and broader multi-asset risk analytics. This instrument/cost breakdown advances only the basic contribution and consistency part of that comparison. [Bloomberg PORT](https://professional.bloomberg.com/products/bloomberg-terminal/portfolio-analytics/).

IBKR documents performance attribution versus a benchmark and broader portfolio measurement. This implementation does not calculate benchmark allocation/selection effects or time-weighted returns with external flows. [IBKR PortfolioAnalyst capabilities](https://www.interactivebrokers.com/en/portfolioanalyst/features.php).

Qualified benchmark/sector/factor inputs, corporate actions and income, FX/multi-asset accounting, actual account execution attribution, external broker reconciliation, prospective performance and target-host operations remain open. Neither this feature nor the four-product comparison establishes positive expectancy or complete vendor parity.
