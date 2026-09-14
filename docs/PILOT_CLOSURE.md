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
| P06 | Complete private deployment | Container config, secrets, health, backup/restore and rollback instructions | Partial: configuration and local DB restore verified; target deployment pending |
| P07 | Authenticated dashboard and controls | Authentication, CSRF, rate/size limits, audit trail | Verified for private founder scope |
| P08 | Interactive market watch | Search, sort, selection, saved watchlist, detail chart, timestamps | Browser verified |
| P09 | Grounded AI copilot | Persisted conversations, provider errors, source context, no execution tools | Verified including one real Claude request |
| P10 | Risk/scenario and execution views | Portfolio scenarios, holdings, fill/proof linkage, costs, exports | Verified basic pilot views; broader risk parity open |
| P11 | Evidence and promotion workflow | Reproducible evaluation, protected external gates, no manufactured readiness | Framework verified: causal baseline, hashes and release-bound review; AI evidence pending |
| P12 | End-to-end and responsive verification | Meaningful regression suite + browser flow on populated fixtures | Local UI/build verified; target-host E2E pending |
| P13 | Runtime strategy/review binding | Effective configuration and source fingerprints; fresh registry evidence, drift halt and canonical fill linkage | Engineering verified for built-in pilot; target-host review pending |
| X01 | Real-feed session observation | Founder/provider feed during an open session; source freshness and sample coverage | External evidence needed |
| X02 | Sustained operational burn-in | Successful token renewal, independent alert and recovery/restore drill in target deployment | External evidence needed |
| X03 | Strategy effectiveness | Holdout and forward-paper evidence; current 100 trades / 30 days minimum is not proof alone | External evidence needed |

Completion is reported separately for engineering and external evidence. Unknown or absent evidence is never green. Real-money execution, broader assets/currencies, and a public multi-tenant product are separate releases.

Detailed results and limitations: [verification record](PILOT_VERIFICATION.md). Deployment and research commands: [private pilot runbook](PRIVATE_PILOT_RUNBOOK.md).

## Wider capability closure remains in scope

The private pilot is the first launch stage, not a replacement for the requested benchmark capability objective. Full broker lifecycle/reconciliation, multi-currency/contracts, advanced portfolio risk/attribution, execution realism, AI validation and hosted interactive parity remain tracked as unfinished work. No aggregate 100% result is implied by the narrower engineering acceptance register.

Lower-timeframe protective-exit replay has now been implemented and regression-tested; [intrabar execution rules](INTRABAR_REPLAY.md) list its precise assumptions and outstanding real-data qualification. This partially advances the TradingView execution-realism comparison, without claiming complete parity.
