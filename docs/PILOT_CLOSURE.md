# Pilot closure contract

Target: 100% verified readiness for a private, paper-only, single-currency NSE cash-equity/ETF pilot. This does not claim parity with every product feature or authorize real-money trading. Capability references and the wider backlog are in `docs/PLATFORM_BENCHMARK.md`.

## Acceptance register

| ID | Requirement | Required evidence | State |
|---|---|---|---|
| P01 | Enforced currency / instrument scope | Reject mixed currencies, unsupported contracts, symbol/asset-class mismatch and mismatched persisted accounts | Engineering verified; legacy symbol-only scope must be reconfigured before buys |
| P02 | Independent protection and operator halt | Exit/halt while analysis is blocked; durable restart behavior; explicit stops and whole-book coverage at final entry boundary | Engineering verified; live-session observation pending; coverage details in P24 |
| P03 | Shared live portfolio valuation | UI/engine parity, staleness and ledger-version checks | Engineering verified; open-session parity pending |
| P04 | Honest strategy performance | Actual account samples, cost-aware metrics, source labels | Engineering verified; forward evidence pending |
| P05 | Honest data/provider readiness | Missing inputs abstain; stale data blocks risk | Engineering verified; source completeness pending |
| P06 | Complete private deployment | Container config and feature/source bindings, secrets, health, backup/restore and rollback instructions | Partial: configuration and selected operational/research restore verified; target deployment/off-host qualification pending |
| P07 | Authenticated dashboard and controls | Authentication, CSRF, rate/size limits, audit trail | Verified for private founder scope |
| P08 | Interactive market watch | Search, sort, selection, saved watchlist, detail chart, timestamps | Browser verified |
| P09 | Grounded AI copilot | Persisted conversations, provider errors, source context, no execution tools | Verified including one real Claude request |
| P10 | Risk/scenario and execution views | Portfolio scenarios, holdings, fill/proof linkage, costs, exports | Verified basic pilot views; broader risk parity open |
| P11 | Evidence and promotion workflow | Reproducible evaluation, protected external gates, no manufactured readiness | Framework verified: causal baseline, release-bound review and SHA-bound holdout/forward/stress/calibration evidence; real AI evidence pending |
| P12 | End-to-end and responsive verification | Meaningful regression suite + browser flow on populated fixtures | Local UI/build verified; target-host E2E pending |
| P13 | Runtime strategy/review binding | Effective configuration and source fingerprints; fresh registry evidence, drift halt and canonical fill linkage | Engineering verified for built-in pilot; target-host review pending |
| P14 | Strategy-specific evidence coverage | Exact episode proof/configuration linkage, unresolved-loss coverage, qualified days and review metric/hash parity | Engineering verified; real strategy validation pending |
| P15 | Usable experiment comparison | Private report publication, candidate coverage/cost sensitivity, authenticated export, responsive Research and Atlas context | Engineering/browser verified on synthetic evidence; real candidate evaluation pending |
| P16 | Provider and market-event research | Provider receipts, continuous portfolio journal, time-aware company-event evidence, real source/provider qualification | Framework/tests integrated; continuous replay and company-event UI verified on synthetic evidence; two-provider/live-source qualification pending |
| P17 | Continuous portfolio research workspace | Read-only journal snapshot, gap-aware curves, candidate holdings/orders, private export and bounded Atlas context | Engineering/browser verified on synthetic evidence; real input and target-host research-recovery qualification pending |
| P18 | Research recovery continuity | Explicit database/report/input/receipt inventory, trusted hashes, deterministic replay and preserved pending-request guards | Engineering/CLI verified on synthetic evidence; complete deployed inventory and off-host restore pending |
| P19 | Company announcement workflow | Search/watchlist, dated revisions, append-only mapping review, private export and cutoff-scoped Atlas | Engineering/browser/API verified on synthetic evidence; real feed and independent mapping qualification pending |
| P20 | Reconciled research contribution | Instrument P&L including closed positions, spread/slippage/fees bridge, independent accounting validation, UI/export/Atlas | Engineering/browser/API verified on synthetic evidence; benchmark/sector/factor attribution remains open; recorded account covered by P21 |
| P21 | Recorded paper-account contribution | One valuation/account/fill/cost read snapshot, reconciled instrument P&L, explicit gaps, current-total withholding, private UI/export/Atlas | Engineering/browser/API verified with the real paper broker on synthetic evidence; real-source, target-host and broader attribution qualification remain open |
| P22 | Historical portfolio-risk workspace | Complete common-session history, covariance/correlation/contributions, empirical tail losses, private UI/export/Atlas and calendar binding | Engineering/browser/API verified; real 11-instrument history alignment checked after documented Budget-session correction; adjustment, calendar completeness and forward risk qualification remain open |
| P23 | Deployment image and heartbeat verification | Actual Dockerfile builds, private shared-state/authentication/restart checks, fail-closed local protection health | Health and local synthetic flow verified; exact Linux image verification tracked by CI; intended-host qualification pending |
| P24 | Stored position protection and final entry boundary | Explicit valid stops, exact asset class, whole-book coverage, persisted halt, atomic rejection, independent exits and current dashboard evidence | Engineering/API/browser verified on synthetic evidence; real-session and target-host qualification pending |
| P25 | Invalid accounting and valuation isolation | Exact quantities, finite amounts, atomic rejection, independent valid exits, unavailable totals, preserved chart/day gaps and durable restart halt | Engineering/API/browser verified on synthetic evidence; source integrity and target-host recovery qualification pending |
| P26 | Source timestamps and ordered quote observations | Actual SDK timestamp conversion, future/late/duplicate rejection, immutable closed bars, unchanged volume baseline and UI/Atlas diagnostics | Engineering/API/browser and offline SDK verified; real session/source completeness and clock qualification pending |
| P27 | Recorded account benchmark comparison | Reconciled historical fills/cash/fees, matched daily closes, bounded gap-aware metrics, private UI/export/Atlas | Engineering/API/browser verified with real paper-broker synthetic history; source, total-return/corporate-action, sector/factor and forward qualification remain open |
| P28 | Separate paper and external broker state; read-only daily order/trade inspection | Exact account identity, bounded full position reads, preserved quantities, independent consistency checks, private UI/export/Atlas | Engineering/API/browser verified on synthetic evidence; real broker lifecycle and broader reconciliation remain open |
| P29 | Durable external observation history and selected review | Atomic retained captures, independent temporal checks, past-issue preservation, private historical export/Atlas and selected recovery | Engineering verified on synthetic evidence; production/browser/image evidence tracked per revision; actual broker lifecycle/source completeness remain open |
| P30 | Recorded paper versus retained historical replay | Unique run/source/configuration retention, explicit time grid, independently checked accounting, fill/curve differences, private UI/export/Atlas and recovery | Engineering verified on synthetic evidence; production/image evidence tracked per revision; same-strategy/OOS and real-source qualification remain open |
| P31 | Benchmark return attribution workspace | Reconciled allocation/selection/interaction, exact input hash, period and portfolio labels, private display/export/Atlas and recovery inventory | Calculation/reader/type tests passed; container, browser and source qualification remain pending; [scope](BENCHMARK_ATTRIBUTION.md) |
| P32 | Selected external broker funds/net-position observation | Profile before/after, repeated funds and position reads, account-bound hash, private Activity/export/Atlas, source scope and freshness | Engineering and browser/CI verification pending; real account, depository holdings, settlement and continuous cash/position reconciliation open; [scope](EXTERNAL_ACCOUNT_SNAPSHOT.md) |
| X01 | Real-feed session observation | Founder/provider feed during an open session; source freshness and sample coverage | External evidence needed |
| X02 | Sustained operational burn-in | Successful token renewal, independent alert and recovery/restore drill in target deployment | External evidence needed |
| X03 | Strategy effectiveness | Holdout and forward-paper evidence; current 100 trades / 30 days minimum is not proof alone | External evidence needed; `pramana.ai_holdout.v1` now provides fail-closed AI holdout evidence plumbing |

Completion is reported separately for engineering and external evidence. Unknown or absent evidence is never green. Real-money execution, broader assets/currencies, and a public multi-tenant product are separate releases.

Detailed results and limitations: [verification record](PILOT_VERIFICATION.md). Deployment and research commands: [private pilot runbook](PRIVATE_PILOT_RUNBOOK.md).

The AI-specific holdout evaluator is documented in [AI holdout evidence](AI_HOLDOUT.md). It validates frozen decision/provenance inputs and after-cost arithmetic, but it cannot establish forward performance or independent calibration without real reviewed evidence.

## Wider capability closure remains in scope

The private pilot is the first launch stage, not a replacement for the requested benchmark capability objective. Full broker lifecycle/reconciliation, multi-currency/contracts, advanced portfolio risk/attribution, execution realism, AI validation and hosted interactive parity remain tracked as unfinished work. No aggregate 100% result is implied by the narrower engineering acceptance register.

Lower-timeframe protective-exit replay has now been implemented and regression-tested; [intrabar execution rules](INTRABAR_REPLAY.md) list its precise assumptions and outstanding real-data qualification. This partially advances the TradingView execution-realism comparison, without claiming complete parity.

## Implementation delta — continuous portfolio research

The private Research page now displays per-candidate equity/drawdown curves, gaps, holdings, fees, fills and pending/cancelled orders from a consistent read-only journal export. Candidate/chart switches, pagination, authenticated download and Atlas handoff are verified on desktop/mobile. [Publication, evidence and limits](PORTFOLIO_RESEARCH_WORKSPACE.md). This closes the initial continuous replay display gap; it does not close real-input qualification, portfolio attribution, paired-provider performance or complete research-state recovery. Schema 2 now captures explicitly selected research journals, reports and receipt directories; no automatic source discovery is implied.


## Implementation delta — selected research recovery

Schema 2 recovery adds the three research database kinds, published files and receipt/input directories to the same manifest as the paper state. It verifies database integrity, all-row hashes and deterministic experiment/portfolio reports, preserves pending provider guards, and fails on source changes or replay drift. [Capture, restore and precise limits](RECOVERY_BUNDLE.md). Local CLI and restored dashboard-reader checks pass. Actual inventory completeness, encrypted off-host retrieval, target-host restart and operator recovery acceptance remain unclosed.


## Implementation delta — company announcement workflow

The private Markets screen now connects stored announcements, watchlist filtering, historical revisions, mapping review and private export. Atlas requests carry a server-enforced company-evidence cutoff and persist that context; historical requests exclude later records and prior chat. Python and dashboard readers agree on correction and conflict eligibility. [Workflow, evidence and limits](COMPANY_EVENT_WORKSPACE.md). This supersedes the initial event-dashboard wiring gap. Real capture coverage, source rights, independent mapping qualification and advanced company/portfolio analytics remain open.


## Implementation delta — mapping lifecycle

Current mappings can now be withdrawn and later re-reviewed without erasing history. Withdrawn/conflicting links are excluded from automatic company context, historical availability preserves microseconds, and recovery retains withdrawal rows. Desktop/mobile, CLI and private API checks pass on synthetic evidence. [Workflow and precise scope](COMPANY_MAPPING_LIFECYCLE.md). P19's mapping-revocation engineering gap is closed; actual source/mapping qualification and the overall external/benchmark gates remain open.

## Implementation delta — research instrument and cost contribution

Continuous research reports now explain instrument P&L, including closed positions, and reconcile execution-reference P&L through spread/slippage/fees to net equity change. Python and Node reconstruct the accounting; stale marks withhold aggregate results. The private dashboard, export and Atlas carry the validated breakdown. [Method and verification](PORTFOLIO_CONTRIBUTION.md). Basic recorded-account contribution is covered by P21 below; benchmark/sector/factor analysis, source qualification and broader platform parity remain open.


## Implementation delta — recorded paper-account contribution

The Portfolio page now joins the current engine valuation to account, positions, fills and fees in one read transaction. It reconstructs immediate fee expense, retains closed losses, exposes missing marks/cost records and withholds current totals on inconsistent/outdated evidence. Search, sorting, pagination, export and bounded Atlas context are connected. [Method and evidence](PAPER_ACCOUNT_CONTRIBUTION.md). This closes the basic recorded-account contribution workflow locally; external broker analysis, benchmark/sector/factor attribution and real pilot qualification remain open.


## Implementation delta — historical portfolio risk

Risk lab now provides exploratory covariance, correlation, volatility contribution, historical VaR/expected shortfall and dated repricing scenarios. Missing holding/session data withholds aggregate numbers; cash stays in the denominator. Desktop/mobile controls, private export and Atlas context are verified. One real source capture exposed the omitted NSE Budget Sunday; the verified exception is now included in the shared calendar and runtime fingerprint. [Method, source observation and limits](HISTORICAL_PORTFOLIO_RISK.md). This partially closes empirical cash-portfolio risk analytics; source adjustment, prospective calibration, sector/factor/Greeks and multi-asset risk remain open.


## Implementation delta — deployment feature bindings

Compose now forwards private comparison, continuous replay and company-event paths, honors selected research/review paths and a custom read-only directives file, and passes the same holiday additions to engine and collector. Risk history records unverified closure additions and preserves conflicting bars for rejection. The recovery example includes the source market snapshot. Container verification uses the resolved service environment and exercises populated private reports/exports, missing-source failures and restoration. [Contract and remaining gates](DEPLOYMENT_WIRING.md). This addresses deployment omissions under P06/P23; it does not qualify the real target host or source/strategy evidence.

## Implementation delta — protection coverage

The final pilot broker now enforces explicit stops, simulated-fill geometry, persisted entry halts and exact symbol/asset-class identity. Missing or corrupt held protection and failed exits durably halt new risk while valid covered exits remain available. Research and Atlas expose the bounded, current ledger-bound coverage report separately from heartbeat/reconciliation. [Behavior, migration and verification](PROTECTION_COVERAGE.md). P01/P02/P24 are strengthened; external qualification and full benchmark closure remain open.

## Implementation delta — ledger integrity

Malformed stored quantity/basis/cash can no longer be rounded, silently projected or committed into a fill. Complete valuations fail closed while valid independent protective exits remain available when account cash is usable. The engine publishes unavailable totals and retains invalid minute/day evidence across restoration; the UI preserves curve gaps and keeps the halt visible. [Exact scope, restart behavior and verification](LEDGER_INTEGRITY.md). P25 strengthens capital-accounting integrity without qualifying external sources, operations or strategy performance.

## Implementation delta — source timing and bar ordering

The Zerodha adapter preserves actual SDK exchange timestamps across host timezones. Future, older, duplicate and invalid updates cannot refresh the latest quote, change sealed bars or corrupt later volume baselines. Markets and persisted Atlas context expose process-scoped rejection diagnostics separately from collector prices. [Contract and verification](TICK_INTEGRITY.md). This strengthens P03/P05/P26; real open-session observation, source completeness and full broker/benchmark parity remain open.

## Implementation delta — recorded account benchmark comparison

Portfolio now reconstructs each historical session book from actual recorded paper fills and fees and compares matched dated closes with NIFTY 50 or NIFTY BANK. Closed losses and cash remain included; missing prices withhold the affected range. Benchmark/window selection, rebased charts, return/drawdown/relative-risk metrics, daily holdings inspection, private export and bounded Atlas context are wired and verified. [Method and precise limits](ACCOUNT_BENCHMARK.md). P27 closes the basic account benchmark workflow, while adjusted total-return data, allocation/selection and sector/factor attribution, independent source qualification and overall launch acceptance remain open.


## Implementation delta — external broker observations

The execution-facing adapters now keep account reads on the paper ledger. Explicit Kite/IBKR reads preserve contract identity and fractional quantities, and the private Activity view inspects bounded Kite daily order/trade captures. [Definitions, migration and limits](BROKER_OBSERVATIONS.md). This advances external observation and mismatch detection; it does not close live order lifecycle, position/cash reconciliation, QuantConnect live/backtest parity or external pilot gates.

## Implementation delta — retained broker lifecycle observations

P29 adds an append-only journal, independent Python/TypeScript lifecycle replay, historical Activity selection, capture-bound Atlas context and schema 2 recovery. Missing records and earlier contradictions remain visible after later recovery. Terminal pending semantics now follow the official Kite cancelled example. [Contract, retention bounds and remaining gates](BROKER_LIFECYCLE_HISTORY.md). This closes selected observation retention/review plumbing, not acknowledged OMS completeness, source coverage or target-host acceptance.

## Implementation delta — recorded paper/replay diagnostics

P30 adds retained harness runs and an explicit-window comparison of independently checked paper/replay observations. Gaps and clock mismatches withhold aggregate returns; fill groups and component differences remain inspectable. Private Research, hash-bound export/Atlas and selected replay-ledger recovery are wired. [Contract, bounds and evidence](RUN_COMPARISON.md). This advances the QuantConnect comparison workflow while same-strategy parallel replay, real sources and overall acceptance remain unqualified.

## Implementation delta — mature observation history

P04/P30 now handle recent comparisons on accounts with longer retained histories. Windowed capture preserves pre-window fills, gaps and explicit selection counts; qualification readers stream rows, reject duplicate UTC buckets and retain invalid-day evidence. Malformed clocks or starting balances withhold current performance and show a Research explanation. [Exact scope and regression evidence](OBSERVATION_HISTORY.md). No retention deletion or expansion of qualified execution/strategy scope is implied.

## Implementation delta — current-state freshness

Current runtime, feed, portfolio, manifest, reconciliation and market-collector evidence is now aged at read time. Engine ticks require a current paper heartbeat, accepted source timestamp and unique instrument identity; held marks and cached workspace claims expire independently. Markets shows per-instrument age and rejection reasons, while expired current claims fail closed and historical reports remain dated. [Freshness contract and bounds](FRESHNESS.md). This closes stale-flag and stale-cache ambiguity in the pilot UI; real-session continuity, host-clock and target-host acceptance remain open.

The external launch gates now have a fail-closed evidence preflight. X01 real-feed observation, X02 target-host burn-in/recovery and X03 AI strategy effectiveness each require attached evidence, reviewer, release revision, target host and timezone-aware observation time; missing evidence cannot be interpreted as acceptance. [Runbook command](PRIVATE_PILOT_RUNBOOK.md). This adds accountability without enabling live execution.

External gate reports now retain each gate's SHA-256 evidence binding in the serialized report, and both Python and dashboard readers require a full 40-character release revision and non-empty target host. A generated report can therefore be consumed by the dashboard without losing its evidence identity; malformed or detached reports remain pending.
