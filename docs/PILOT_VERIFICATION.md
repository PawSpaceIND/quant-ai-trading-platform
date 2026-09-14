# Pilot implementation verification — 14 September 2026

Initial base: `83a1161` (PR #48); subsequently integrated `d3b5bb8` (PR #49) to preserve the private Cloudflare viewer. Implementation branch: `codex/pilot-readiness-closure`.

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
- Docker is not installed in this environment. Compose/Dockerfiles were prepared and reviewed but were not executed. The new upstream deployment uses a Mac engine and Cloudflare read-only viewer. Always-on engine hosting remains unqualified.

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

The Cloudflare compatibility update passes static export and Worker authentication/read-only tests. Hosted interaction is deliberately limited to observation: the full copilot, saved-watchlist edits and halt control are available on the authenticated engine workspace. This is not full cloud feature parity.

Cloudflare's local Worker/D1 emulator was also exercised with isolated synthetic snapshots: ingest returned 200, unauthenticated workspace access returned 401, and authenticated access returned 200. The static dashboard rendered the hosted read-only banner, snapshot valuations and disabled mutation controls. No changes were deployed to the remote Worker or D1 database.

## Intrabar execution follow-up

The Python suite now passes **317 tests** (16 additional intrabar cases); Ruff and diff whitespace checks pass. The replay accepts validated lower-timeframe windows, resolves chronological protective triggers, records conservative same-bar ambiguity, uses opening prices for gap exits plus broker friction, gives existing opening protection priority, and clears historical execution context after errors. JSON import and tearsheet export are covered. See [execution rules and limitations](INTRABAR_REPLAY.md).

These are synthetic engineering checks. No real intrabar dataset or AI-strategy effectiveness claim was added. Prior-close features with next-open sizing remain explicitly distinguished from orders frozen at the prior close. The broader benchmark requirements and all external evidence gates remain open where previously marked open.

## Independent monitoring follow-up

The Worker now exposes a separately authenticated `/healthz` endpoint. It ages the engine's original heartbeat independently of upload timestamps, fails on unknown/halted state, and returns 503 on missing or failed storage evidence. The monitor token has no portfolio or ingest access. All **12 Worker tests** pass (four added monitoring tests with multiple failure cases).

The local Worker/D1 HTTP drill verified missing snapshot 503, current evidence 200, fresh publication with stale engine 503, engaged halt 503, and recovery 200. Synthetic evidence only; no external notification was sent and no remote service was changed. [Independent monitor deployment and acceptance instructions](INDEPENDENT_MONITOR.md). X02 remains open until target-host and received-alert evidence exists.
