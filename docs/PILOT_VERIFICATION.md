# Pilot implementation verification — 14 September 2026

Base: `83a1161` (main, PR #48). Implementation branch: `codex/pilot-readiness-closure`.

## Verified engineering behavior

- 301 Python tests passed; Ruff passed. Added regression coverage for persistent INR scope, stale prices, post-analysis price movement, entry halts versus exits, protection during a blocked event loop, telemetry, WAL-aware backup, and causal research splits/path stress.
- 10 dashboard server tests passed. They cover session tampering/rotation, origin checks, bounded requests, persistent rate limits, ledger-version invalidation, missing-provider and successful persisted copilot responses, malformed market rows, interrupted requests, and complete/consecutive account-performance sessions.
- Next.js production build and TypeScript checks passed. CI now runs a production build and the UI tests using Node 24.
- Browser verification used an isolated, clearly labelled synthetic ledger/collector. Login, overview, market search, price sorting, saved-list persistence after reload, selected-instrument charts, holdings, risk-slider calculations, scenario-to-copilot prompt, saved provider-error history, fill inspection with honest missing-proof display, halt request and acknowledgement, research report display, and sign-out controls were inspected. Escape dismisses the halt dialog and returns focus to its trigger.
- Desktop and 390-pixel phone layouts were inspected. Phone document width equalled viewport width; scrollable tables stayed contained. No browser error/warning logs were captured. The mobile copilot has its own close control before returning to navigation.

## Provider and operational evidence

- Real Zerodha authentication/REST collection succeeded: 13 records returned. Session was CLOSED; an INFY last-trade timestamp was 11 September. Epoch-zero index timestamps are removed by the dashboard reader. Collector retrieval time is not presented as proof of fresh market trading.
- Real local environment initialization succeeded in an isolated ledger: INFY, RELIANCE and TCS, INR/NSE/equity, protection heartbeat persisted, zero orders. No live-order endpoint was enabled. This check did not start a websocket feed and does not prove open-session stream coverage.
- One actual Claude request used the new persisted copilot service with a synthetic QA context. It completed, saved its answer and source context, and recorded 4,294 input / 90 output tokens. The browser displayed that saved result. Cost was not estimated because a versioned pricing table is not configured.
- A consistent SQLite backup and non-destructive restore drill completed on the isolated runtime ledger. Backup and restored database SHA-256 matched: `39bb603f7ed5278837f942aabf60eb931d16bd45fe3f09605957417a45bd9516`. This does not constitute a complete target-host disaster-recovery drill including proofs, console, secrets, alerts and rollback.
- Docker is not installed in this environment. Compose/Dockerfiles were prepared and reviewed but were not executed. The intended production host remains unconfirmed.

## Research workflow evidence

A single deterministic SMA baseline experiment used 746 actual Zerodha INFY daily bars with training-only window selection, disjoint walk-forward intervals and a final 40-observation holdout. Cost assumption: 20 bps per unit of turnover, including closing the test position. Holdout net return: −2.03%; buy-and-hold: −5.74%; holdout drawdown: 8.85%. Moving-block bootstrap p95 drawdown: 18.61% (hypothetical).

This is a baseline experiment, not AI-swarm performance. Corporate-action adjustment was not independently verified. Close-to-close marks and approximate costs are not intrabar/broker execution models. No promotion was approved. The tested holdout has now been inspected; it must not be reused as an untouched final validation set.

## Open closure work

1. Qualify the target deployment: container startup, TLS/private network, independent alert receipt, credential renewal, resource/soak behavior, full bundle recovery and rollback.
2. Observe real websocket coverage and protective behavior during an open NSE session. 14 September 2026 is a trading holiday; source: [NSE circular CMTR71775](https://nsearchives.nseindia.com/content/circulars/CMTR71775.pdf).
3. Gather sustained forward-paper history. The dashboard only counts completed sessions with at least 300 fresh minute observations and late-session coverage. Sharpe/Sortino require 20 consecutive-session returns; sample count alone never proves an edge.
4. Complete AI-specific holdout/walk-forward evaluation, calibration/drift comparison, realistic intrabar cost/fill stress and independently reviewed strategy evidence. The new deterministic experiment is a foundation, not closure of those requirements.
5. Complete and sign the release-bound operator reviews using `quant_ai.governance.pilot_review`. The workflow rejects incomplete evidence, failed strategy policy and baseline-only submissions; the dashboard rejects altered, expired or mismatched signatures. No real acceptance has been signed in this task. Real-money execution stays physically disabled.
6. Broader benchmark parity remains open: live broker lifecycle/reconciliation, multi-currency cash accounting, asset-contract support, correlation/sector/factor/Greeks risk and Bloomberg-style attribution are separate engineering work. Current UI scenarios are basic linear shocks.

The 100% closure goal remains active. No full-pilot, production-launch, profit or benchmark-parity certification is issued by this verification.
