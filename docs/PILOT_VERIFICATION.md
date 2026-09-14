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

## Portfolio risk workspace follow-up

The risk lab now supports per-holding cash-equity/ETF shocks with reset to a common shock, scenario contribution/equity impact, largest holding/equity, gross exposure/equity, effective holding count (inverse squared invested weights), and downside to recorded stops with explicit missing/breached-stop warnings. Invalid geometry, duplicate holdings, mixed markets, contracts and shorts fail closed. Expired portfolio snapshots cannot retain fresh-mark labels.

All **15 dashboard tests** pass, including five new risk-diagnostic cases. TypeScript and production build passed. Synthetic browser checks verified the individual override, reset, exact scenario handoff to the Atlas prompt and concentration/stop outputs. A phone overflow found during inspection was fixed using the existing table scroll container; document and viewport width both measured 390 pixels. No browser error/warning logs were observed. The last freshness predicate change is regression-tested; layout and prompt code were unchanged afterward.

These descriptive measures do not calculate covariance, factor/sector risk, VaR, Greeks or attribution, and stop distance is not a guaranteed loss ceiling. They advance portfolio inspection and what-if capability without claiming full IBKR/Bloomberg parity. Browser verification used synthetic holdings and did not send an additional provider request.

## Paper-account reconciliation follow-up

All **331 Python tests** pass (14 added cases), Ruff passes, and all **15 dashboard tests** pass. The default local Turbopack build hit sandbox port-binding restrictions; the supported webpack production build and TypeScript passed. Browser checks verified that a matched report is shown as observed and a synthetic mismatch report is shown as not verified, with no browser warnings/errors. A read-only CLI drill matched one synthetic ledger fill and seven cost rows.

The new check independently reconstructs internal paper cash, quantities, average prices and recorded protection. Corruption tests cover non-repair, tenant isolation and persistent entry halt; direct pilot buys also enforce the check. Later fills invalidate the prior checked ledger version. [Reconciliation rules and limits](PAPER_RECONCILIATION.md). This does not close external broker lifecycle/cash/position reconciliation.

## Cross-file recovery follow-up

The suite now passes **341 Python tests** (ten added bundle cases), with Ruff and diff checks passing. A stopped-writer bundle captures both databases, proof/review directories, directives and optional halt/research files. Source fingerprints detect capture-time changes; restore requires an independently retained manifest hash, verifies every copied file and checks internal accounting and order-ID proof coverage. SQLite backup connections now close explicitly and snapshots use standalone journal mode before hashing; this fixes transient sidecar files entering an inventory.

The local synthetic QA drill restored one saved preference, two conversations, five audit records and the halt file. Paper accounting matched. Its one QA fill has no XAI proof, so the tool correctly retained the restored evidence with `discrepancy` and CLI exit 2. This is detection of an expected fixture gap, not full recovery approval. [Recovery workflow and limitations](RECOVERY_BUNDLE.md). Target deployment, encrypted off-host retrieval, secret/configuration recovery, startup/rollback and independent alerts remain unverified.

## Completed-trade evidence follow-up

All **348 Python tests** pass (seven added cases), Ruff and TypeScript pass, and all **15 dashboard tests** pass. Local webpack production build passed. Flat-to-flat paper episodes now distinguish fill count from completed trades, include recorded cash fees and disclose open episodes separately. The source-hashed CLI artifact and bounded telemetry summary are wired into Research and the evidence export. Forward observation additionally requires at least 100 ledger-derived completed episodes.

Synthetic browser/CLI checks showed one fill, one open episode, zero completed trades, unavailable completed-trade mean/win rate and disclosed open fees. Phone width and document width both measured 390 pixels; no browser warnings/errors were observed. The final fee-refresh change was regression-tested afterward without UI changes. [Counting rules and qualification limits](TRADE_EPISODE_EVIDENCE.md). This does not establish AI strategy attribution, calibration, forward provenance or profitability.

## Protective-exit evidence follow-up

All **353 Python tests** and **16 dashboard tests** pass, with Ruff, TypeScript and local webpack production build passing. Protective paper fills now atomically persist their deterministic evidence and cooldown with the ledger transaction. Injected storage failure rolls back cash, positions, costs, fill and cooldown. Recovery coverage includes tenant/order-matched ledger records.

The isolated Activity browser drill showed the protective SELL with threshold, observed mark and an explicitly unverified custom source. The unproven synthetic BUY stayed labeled Missing proof. No warning/error browser logs were observed. No real-market or deployment acceptance is implied. [Evidence fields and limits](PROTECTIVE_EXIT_EVIDENCE.md).

## Governed swarm-fill durability follow-up

All **359 Python tests** and **17 dashboard tests** pass, with Ruff, TypeScript and local webpack production build passing. Governed swarm fills now commit their canonical decision record and replay key with cash, positions, fees and the fill. Tests include an abrupt subprocess exit after commit, evidence-insert rollback, preparation failure and file-projection failure. The restart replay guard allows no second fill for the same key.

With no file projection present, the synthetic browser drill showed the exact fill/rationale/verdicts in Activity and the saved input agent in Overview. No warning/error browser logs were observed. A subsequent stopped-writer bundle restored one decision record with matched accounting and zero missing proof references; the initial source-changing capture was rejected. [Transaction, evidence and scope limits](SWARM_FILL_EVIDENCE.md). Target-host and strategy acceptance remain pending.

## Decision-provenance follow-up

All **366 Python tests** and **18 dashboard tests** pass, with Ruff, TypeScript and local webpack production build passing. Atlas policy/input snapshots and per-call SDK request/model/status metadata now persist through proposal, XAI trace and the atomic fill record. Tests exercise reverse-order concurrent completion, timeout/schema failure, missing returned identity and exact hash/ledger propagation.

The isolated mocked-SDK browser drill showed requested and returned identities separately, an explicit test/custom transport label and exact fingerprints. The mobile proof panel was corrected to fit the visible journal: viewport/document width 390 pixels, detail width 308 pixels. Desktop Overview and Activity also passed, with empty warning/error logs. Recomputed request/configuration/input hashes matched. [Fields, privacy and remaining qualification](DECISION_PROVENANCE.md). This is partial provenance, not a complete strategy registry or proven AI performance.

## Runtime strategy binding follow-up

All **381 Python tests** and **21 dashboard tests** pass, with Ruff, TypeScript and the local webpack production build passing. The engine now records its effective built-in configuration, source inventory, selected dependency versions and release declaration in a canonical manifest registry. A changed or unavailable record durably halts entries; protective exits still run. Exact checked summaries reach canonical fill evidence. Strategy review requires fresh engine/registry agreement, in addition to signed artifact and configured-hash checks.

The isolated factory/browser drill showed a matched configuration, then a changed configuration and acknowledged entry halt after a runtime setting mutation. No fills, real feed connections or model requests were made. Source scans covered the actual local package; the declared base revision plus uncommitted changes is disclosed in the retained drill. Cached manifest checks had a 0.72 ms median and 2.04 ms maximum across 100 local samples; a forced scan took 12.64 ms. These are local observations, not a production latency guarantee.

Desktop viewport/document widths were 1280/1280 and phone widths 390/390; readiness cards wrapped correctly. Workspace warning/error logs after reload were empty. The first synthetic login correctly rejected a mismatched origin; configuring the exact local origin resolved it. [Runtime manifest fields, export and limits](RUNTIME_STRATEGY_MANIFEST.md). This closes the built-in runtime-binding implementation gap, while target-host qualification, full AI provenance/effectiveness and real operator review remain open.
