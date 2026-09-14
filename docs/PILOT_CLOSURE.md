# Pilot closure contract

Target: 100% verified readiness for a private, paper-only, single-currency NSE cash-equity/ETF pilot. This does not claim parity with every product feature or authorize real-money trading. Capability references and the wider backlog are in `docs/PLATFORM_BENCHMARK.md`.

## Acceptance register

| ID | Requirement | Required evidence | State |
|---|---|---|---|
| P01 | Enforced currency / instrument scope | Reject mixed currencies, unsupported contracts and mismatched persisted accounts | Engineering verified |
| P02 | Independent protection and operator halt | Exit/halt while analysis is blocked; durable restart behavior | Engineering verified; live-session observation pending |
| P03 | Shared live portfolio valuation | UI/engine parity, staleness and ledger-version checks | Engineering verified; open-session parity pending |
| P04 | Honest strategy performance | Actual account samples, cost-aware metrics, source labels | Engineering verified; forward evidence pending |
| P05 | Honest data/provider readiness | Missing inputs abstain; stale data blocks risk | Engineering verified; source completeness pending |
| P06 | Complete private deployment | Container config, secrets, health, backup/restore and rollback instructions | Partial: configuration and selected operational/research restore verified; target deployment/off-host qualification pending |
| P07 | Authenticated dashboard and controls | Authentication, CSRF, rate/size limits, audit trail | Verified for private founder scope |
| P08 | Interactive market watch | Search, sort, selection, saved watchlist, detail chart, timestamps | Browser verified |
| P09 | Grounded AI copilot | Persisted conversations, provider errors, source context, no execution tools | Verified including one real Claude request |
| P10 | Risk/scenario and execution views | Portfolio scenarios, holdings, fill/proof linkage, costs, exports | Verified basic pilot views; broader risk parity open |
| P11 | Evidence and promotion workflow | Reproducible evaluation, protected external gates, no manufactured readiness | Framework verified: causal baseline, hashes and release-bound review; AI evidence pending |
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
| X01 | Real-feed session observation | Founder/provider feed during an open session; source freshness and sample coverage | External evidence needed |
| X02 | Sustained operational burn-in | Successful token renewal, independent alert and recovery/restore drill in target deployment | External evidence needed |
| X03 | Strategy effectiveness | Holdout and forward-paper evidence; current 100 trades / 30 days minimum is not proof alone | External evidence needed |

Completion is reported separately for engineering and external evidence. Unknown or absent evidence is never green. Real-money execution, broader assets/currencies, and a public multi-tenant product are separate releases.

Detailed results and limitations: [verification record](PILOT_VERIFICATION.md). Deployment and research commands: [private pilot runbook](PRIVATE_PILOT_RUNBOOK.md).

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
