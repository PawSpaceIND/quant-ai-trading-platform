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

All **384 Python tests** and **21 dashboard tests** pass, with Ruff, TypeScript and the local webpack production build passing. The engine now records its effective built-in configuration, source inventory, selected dependency versions and release declaration in a canonical manifest registry. A changed or unavailable record durably halts entries; protective exits still run. Exact checked summaries reach canonical fill evidence. Strategy review requires fresh engine/registry agreement, in addition to signed artifact and configured-hash checks.

The isolated factory/browser drill showed a matched configuration, then a changed configuration and acknowledged entry halt after a runtime setting mutation. No fills, real feed connections or model requests were made. Source scans covered the actual local package; the declared base revision plus uncommitted changes is disclosed in the retained drill. Cached manifest checks had a 0.72 ms median and 2.04 ms maximum across 100 local samples; a forced scan took 12.64 ms. These are local observations, not a production latency guarantee.

Desktop viewport/document widths were 1280/1280 and phone widths 390/390; readiness cards wrapped correctly. Workspace warning/error logs after reload were empty. The first synthetic login correctly rejected a mismatched origin; configuring the exact local origin resolved it. [Runtime manifest fields, export and limits](RUNTIME_STRATEGY_MANIFEST.md). This closes the built-in runtime-binding implementation gap, while target-host qualification, full AI provenance/effectiveness and real operator review remain open.

A final configuration review added SDK endpoint/retry/timeout binding and rejected custom protective resolvers/injected inference transports. Protective tenant and broker/feed wiring are recorded too. These additions passed regression tests after the browser drill; the dashboard route/layout did not change.

## Strategy-specific evidence follow-up

All **405 Python tests** and **25 dashboard tests** pass, with Ruff, TypeScript and the local webpack production build passing. Episode attribution now verifies canonical fill/protection evidence against exact runtime manifests, fill timing, fees and tenant identity. Entirely unproven episodes after a configuration starts cannot be used to omit losses from acceptance. Source/evidence fingerprints change with fills, fees, proofs or manifest records.

Configuration-qualified days require 300 distinct fresh minute samples and a late-session sample; legacy, mixed-configuration and incompatible-fill sessions are excluded. Signed reviews must match the strategy evidence fingerprint, trade count, mean P&L, profit factor and recorded days. Account returns remain labeled separately. Regression tests include full factory entry/protective-exit linkage, missing/tampered proofs, stale/after-the-fact bindings, fee correction, duplicate minute samples and review mismatch.

The synthetic browser/CLI drill showed two account trades, one linked trade and one unresolved unproven loss. Account closed P&L was −10.63 INR while linked P&L was +9.66 INR; the unresolved coverage remained Not verified. These are deliberately constructed QA outcomes, not strategy returns. The CLI fingerprint matched runtime evidence. Desktop and phone widths were 1280/1280 and 390/390; no browser warning/error logs were observed. The Research action populated the evidence question in Atlas on both layouts. A separate mocked-provider test confirmed the same evidence hash and qualified-day count in outbound and persisted copilot context. No real provider request was made.

[Ownership, review workflow and limits](STRATEGY_EPISODE_ATTRIBUTION.md). This advances reproducible strategy evaluation; it does not establish genuine forward data, calibrated AI, economic/factor attribution, target-host operation or complete benchmark parity.


## Integrated research lab and private workspace — 14 September 2026

- Incorporated main `3c343221b832d219d266fb44f91daaffcd0f2077` (#51) into the pilot branch, preserving strategy-episode attribution changes.
- Local combined regression: **437 Python tests**, **29 UI tests**, **13 Worker tests** passed. Ruff and TypeScript passed; the webpack production build passed including `/api/research/comparison`. CI results are recorded against the final PR revision separately.
- Research publishing reads an isolated SQLite snapshot, preserves source bytes, writes a new 0600 file, and omits raw inputs/prompts/rationales/arbitrary configuration. Malformed, cross-tenant and tampered published reports are rejected; no report permits promotion.
- Browser drill: a synthetic candidate retained one completed loss, one unresolved exit, one provider error, one pending buy and one missing decision. The loss was −₹12.58 at base costs, −₹16.16 at 2x and −₹19.75 at 3x. Unresolved exits remained unavailable in every scenario. These are fixture calculations, not strategy performance.
- Desktop viewport/document width 1280/1280; mobile 390/390. Cards, disclosures and Atlas prefill were inspected. Table overflow stayed within the panel; programmatic horizontal scrolling was not independently established. Atlas was opened without a model request; a separate mocked transport test verified exact bounded outbound/persisted report context.
- A blob-based download did not yield a browser download event and was replaced with an authenticated attachment endpoint. Final browser attachment download passed. API drill confirmed 401 without a session, 200 with identical dashboard/export content and no private sentinels, and a 503 export for a damaged report while workspace remained 200 and readiness checks were unchanged.
- Source cloud publisher and Worker ingestion regression tests verify that `researchLab` and operator notes are omitted before transmission/storage. The hosted viewer does not expose the comparison endpoint.
- All verification used isolated synthetic research/console storage, empty market/ledger paths and a test session key. No live broker, paid provider call, acceptance signature, deployment or strategy promotion occurred. Temporary server and browser tab were stopped/closed.


## Further integration of main #52 — 14 September 2026

Main `edeba3a` (provider comparison, continuous portfolio research and NSE event evidence)
was merged without conflicts. The combined suite passes **474 Python tests**, **29 UI
tests**, **13 Worker tests**, Ruff and TypeScript; local webpack production build passes.
The case-report publisher/reader/UI now retains unknown API cost counts, marks the known
subtotal incomplete, and separates requested from recorded returned model identities.
The synthetic fixture and consumer tests exercise those fields. A final API drill again
passed authenticated workspace/export equality, unauthenticated 401 and invalid-file
isolation. Final mobile inspection showed $0.003000 as incomplete with one unknown-cost
decision and kept the page within 390px, without console warnings/errors.

#52's real-provider/feed claims are recorded in its upstream verification document;
this integration did not repeat a paid model call or NSE retrieval. Continuous-journal
curves and company-event management remain CLI/API workflows pending further dashboard
integration and external qualification. This is progress toward closure, not full parity.


The first combined CI run at `324e24c` exposed an undeclared production dependency:
`scripts/publish_cloud_snapshot.py` imported `requests`, which existed locally but
was absent after a clean install. The publisher privacy test failed before its mock
could run (473 passed / 1 failed). `requests>=2.32,<3` is now declared in the project
runtime dependencies; the publisher regression is retained. Final CI is checked
on the subsequent committed revision, rather than reusing the earlier local result.


## Continuous portfolio workspace — 14 September 2026

- Combined regression: **485 Python tests**, **33 UI tests**, **13 Worker tests**, Ruff and TypeScript pass. The webpack production build passed again after fixing duplicate compact Y-axis labels on a flat cash curve. Two existing Starlette deprecation warnings remain; final GitHub CI is recorded against the committed revision separately.
- Read-only snapshot/source integrity, independent cash/fee/partial-fill/expiration arithmetic, stale gaps and gap-triggered buy halt/allowed exits pass. Consumer tests reject tenant/hash/schema/current-value/gap/chronology corruption and preserve nulls in bounded outbound and persisted mocked Atlas context. Publisher and Worker tests exclude private portfolio evidence.
- Synthetic browser fixture: 71 events, 34 quotes, 35 orders, 32 fills, four missing valuations, one pending order and three cancellations. Active equity/return/current drawdown remained unavailable; cash-only equity was ₹1,000 with zero return. These are plumbing fixtures, not strategy outcomes.
- Desktop 1280/1280 and mobile 390/390 viewport/document widths; mobile chart 324px. Drawdown SVG retained two separate segments across the internal gap. Fills paginated 25 then 7 rows. Candidate/chart switches, authenticated attachment download, correct selected-candidate Atlas prefill and mobile close passed. Final console: no warnings/errors. No provider message was sent.
- API: unauthenticated workspace/export 401, authenticated attachment 200 identical to workspace report with no private sentinels; damaged report isolated as invalid with export 503 while workspace stayed 200 and readiness checks were unchanged. Original fixture restored.
- Test server/tab stopped/closed and viewport reset. No deployment, real order, paid provider call or signed acceptance. Source hash, publication instructions and remaining research recovery limitations are in [the portfolio workspace record](PORTFOLIO_RESEARCH_WORKSPACE.md).


## Research recovery continuity — 14 September 2026

- Full Python suite: **500 passed**, two existing Starlette deprecation warnings; Ruff passed. No frontend/Worker code changed in this increment. Their preceding 33/13 tests and browser checks remain scoped to that earlier revision; final CI is recorded separately for the committed recovery revision.
- Schema 2 captures selected experiment/portfolio/company-event databases plus receipt/input directories and published files. Tests verify full row/raw-BLOB preservation, supported deterministic report equality, nonempty WAL inclusion, capture-write rejection, invalid inventories, schema/foreign-key failures, tampering, missing receipts, event corruption, replay drift and schema 1 compatibility.
- Explicitly closing the old paper fixture exposed a real false-positive: a read-only open created a zero-byte WAL after the initial inventory. Capture now ignores only empty WALs and still fingerprints nonempty WAL bytes. Closed-writer and committed-WAL scenarios pass; real capture-time data changes still fail.
- Separate synthetic CLI create and restore both exited 0. Six selected research sources restored; published report files were byte-identical. Existing TypeScript dashboard readers loaded both reports and retained stale equity as null. Company mapping availability respected its recorded time. A pending receipt blocked a provider retry with zero provider calls.
- The drill retained the paper halt and passed existing accounting/order-ID coverage checks; this does not strengthen their previously documented authenticity limitations. No source retrieval, paid call, real order, deployment activation or signed acceptance. [Manifest digest, commands and remaining off-host/target-host gates](RECOVERY_BUNDLE.md).


## Company announcement workspace — 14 September 2026

- Combined suite: **503 Python**, **40 UI** and **13 Worker** tests pass; TypeScript, CI-scope Ruff (`src tests`) and webpack production build pass. Two existing Starlette warnings remain. Repository-wide Ruff additionally reports seven pre-existing script findings outside the CI scope (import order and broad exception handling); no whole-repository lint pass is claimed.
- Python-generated fixtures verify six cutoff views, non-resurrection after an unmapped correction, equal-time conflicts, Unicode hashes, raw-capture corruption, mapping concurrency and Python/Node interoperability. Mocked Atlas requests preserve bounded context and exclude later company records and prior conversation from historical review. Cloud publisher/Worker privacy tests exclude company evidence.
- Synthetic Markets browser flow: 19 captures, 18 revisions, 15 events, 10/5 pagination, search, saved watchlist, mapping review/history, historical cutoff/export and Atlas prefill. At 04:00:25Z on 1 January only one event and two revisions are known, with 16 later revisions excluded. A browser-submitted request with an empty provider key saved the exact scoped context and visible provider setup error. Full-current-context reset and mobile close passed; no external provider call.
- Desktop and phone document widths matched 1280/390px viewports. Mobile detail width 324px and final cutoff notice 330px. No browser console warnings/errors. Temporary server and test tabs stopped/closed; viewport reset.
- API results: private reads 401 without a session, mutations 403 without the correct Origin, authenticated attachment 200, mapping stale retry 409, invalid symbol/oversized body 400 and rate limit 429. Corrupt event storage returned 503 while workspace remained 200 and readiness unchanged; original fixture restored.
- Source retrieval, independently reviewed mappings, real provider outcomes and target-host operations remain unqualified. [Exact workflow and limits](COMPANY_EVENT_WORKSPACE.md).


## Mapping withdrawal and timestamp precision — 14 September 2026

- **507 Python**, **43 dashboard** and **13 Worker** tests pass, together with CI-scope Ruff, the changed CLI script, TypeScript and webpack production build. The unchanged Starlette warnings remain. A deterministic fixture reproduces byte-for-byte and yields Python/Node parity across 22 symbol/cutoff snapshots.
- Withdrawal is server-timed, append-only and protected against stale/future/invalid requests. Equal-time mapping conflicts are withheld; later reviews resolve them. Microsecond cutoff comparisons prevent early disclosure or false conflicts after millisecond rounding. Recovery preserves current withdrawal and historical eligibility.
- Synthetic browser verification covers withdrawal from a saved-watchlist view (2 records to 0), persistent confirmation, historical lookup, retained withdrawal reason, later mobile re-review and three-row history. Mobile viewport/document 390px; event detail 324px and action selector 286px. No browser warnings/errors.
- API withdrawal succeeds without market rows; private mutation checks remain 401/403, stale retry 409, duplicate/invalid withdrawal 400 and authenticated export 200. Readiness checks are unchanged. The actual CLI rejects stale reviews and missing stores; Python/Node read one another's withdrawal records. No provider request, real source capture, order, deployment or signed acceptance. [Evidence and limits](COMPANY_MAPPING_LIFECYCLE.md).


## Research instrument and execution-cost contribution — 14 September 2026

- **516 Python**, **46 dashboard** and **13 Worker** tests pass, together with CI-scope Ruff, TypeScript and webpack production build. Two existing Starlette warnings remain. Six shared Python/Node scenarios cover empty, fresh, stale, partly restored, restored and reopened books. Independent arithmetic covers scaled/partial fills, fee-inclusive average cost and a closed losing instrument.
- Recomputed hashes cannot conceal tested accounting/quote/contribution inconsistencies. Net P&L includes costs once, and missing final marks cannot produce aggregate profit. New reports use schema v2; valid v1 accounting remains readable with attribution explicitly absent.
- Synthetic desktop/mobile checks verify impact/cost/symbol sorting, cash candidate, closed-position rows, missing totals, authenticated download and Atlas prefill/submission. Mobile viewport/document 390px; table region and selector 324px. No console warnings/errors. Saved Atlas context matches the report, excludes raw fill/curve arrays and records the expected empty-provider error with zero external calls.
- Private export is 401 unauthenticated and 200 authenticated/no-store, matching the workspace. Malformed report export is 503 while workspace remains 200 and readiness unchanged. All six scenario exports validate; missing account/event stores are not created. No orders, market capture, deployment or acceptance. [Exact accounting bridge, evidence hashes and remaining gaps](PORTFOLIO_CONTRIBUTION.md).


## Recorded paper-account contribution — 14 September 2026

- **517 Python**, **52 UI** and **13 Worker** tests pass, alongside CI-scope Ruff/the changed publisher, TypeScript and webpack production build. The existing two Starlette warnings remain. A shared fixture is generated through the real paper broker, tracker and telemetry, with independent expected cash, weighted entry, immediate fees, scaled exits, closed losses and equity.
- Account/valuation/positions/fills/costs are read in one transaction. A real concurrent WAL writer test proves that one response retains its original coherent ledger and the next rejects the stale valuation. Tests reject malformed/orphan/duplicate/negative costs, incorrect notional/side/scope/tenant/positions/P&L and missing required stores. Invalid/outdated evidence hides current Portfolio/Overview totals and fails the valuation gate.
- Browser/API checks use 55 instruments, 110 fills and 330 cost rows. Search, no-match/clear reset, ordering and 25/25/5 pages pass. Closed losses remain included, partial freshness withholds totals, and the invalid view omits numeric tiles. Desktop/mobile document widths match 1280/390px; mobile table region 324px and input/select 154px.
- Authenticated no-store export matches the workspace's rows/totals/source hash. Unauthenticated export is 401; invalid/outdated exports are 503 while workspace is 200 and the marks gate fails. Incomplete reports retain null current totals. Atlas context prioritizes missing marks, caps rows at 50 with omitted counts, preserves full totals and excludes raw fill/cost arrays. Source publisher and Worker remove private account contribution. [Full method, precise limits and synthetic evidence](PAPER_ACCOUNT_CONTRIBUTION.md).


## Historical portfolio risk and Budget-session correction — 14 September 2026

- **522 Python**, **58 dashboard** and **13 Worker** tests pass, with CI-scope and changed collector/publisher Ruff, TypeScript, webpack build and static export. An independent 120-interval orthogonal-return fixture checks covariance, cash dilution, contributions (including negative offsets), undefined constant-series correlation and fractional signed-loss tails.
- The collector publishes bounded completed-day history, source/token identities and a dated calendar. Missing or malformed holding/date records withhold aggregates; stale valuations and unsupported scopes fail closed. A real capture returned 173 closes for 13 instruments and exposed the missing 1 February session. The official NSE exception is now supported in the shared calendar and strategy fingerprint. The retained real equity/ETF series pass 11-instrument alignment on a synthetic validation book; adjustments/source rights remain unqualified.
- Synthetic desktop/mobile checks verify correlation selection, contribution ordering, scenario pages, missing-data coverage, private download and Atlas. Final API checks cover 172 intervals, independent scalar scenario volatility, matching workspace/export, 401 unauthenticated, 422 incomplete and 503 invalid/stale responses. Invalid history does not alter unrelated readiness controls. The 80-interval report withholds VaR/ES while retaining covariance.
- Mobile viewport/document are 390px, table region 324px and selectors 154px; no console warnings/errors. Final saved Atlas context includes 10 scenarios and 162 explicit omissions, excludes raw risk history and records the expected empty-provider error. Temporary resources stopped; running deployment unchanged. [Complete methodology and evidence](HISTORICAL_PORTFOLIO_RISK.md).


## Deployment image and local protection health increment

The local health command now validates both original timestamps, running paper mode and an explicit boolean halt state. Missing/corrupt/oversized/foreign/stale/stopped evidence fails with bounded JSON and exit 2. A valid halt reports a live protection heartbeat while failing availability, without disclosing the halt reason. Regression cases include the real telemetry publisher and halt recovery. All **545 Python tests** pass with the two existing Starlette warnings; CI-scope Ruff and changed operational scripts pass.

The local synthetic prerequisite used the real paper broker/protection publisher and compiled private dashboard. Authentication, CSRF rejection, two-share account parity, saved INFY watchlist, dashboard halt acknowledgement, persistent halt across engine restart, explicit operator recovery and saved watchlist after dashboard restart all passed. Temporary processes were closed. This did not start provider streams or make AI requests.

A read-only check of the existing Mac pilot ledger found no `pilot_runtime` table. The new probe correctly cannot qualify that legacy deployment; it was not migrated, restarted or activated by this change.

The new `containers` CI job validates Compose configuration and builds both actual deployment images, then runs isolated authentication/shared-volume/halt/restart/staleness checks with networking disabled. Exact results and image identifiers are retained in its artifact. [Contract and remaining target-host gates](CONTAINER_VERIFICATION.md). CI results must be inspected before claiming the image verification passed.

The first Linux run built both deployment images and passed the synthetic checks. Reviewing its retained timestamps exposed a test gap: the halt-after-restart assertion could accept the previous process's heartbeat. The probe now reports the original observation time, and CI requires a post-start heartbeat while the persisted halt is still present. The exact latest CI artifact is required for this stronger restart claim.


## Deployment configuration wiring increment

The default Compose file previously omitted three dashboard feature paths, ignored custom research/review source paths, hard-coded the example directives bind and omitted collector holiday overrides. The dashboard now receives the selected paths, the engine mounts the selected read-only directives, and collector session labels/risk dates use the same merged closure list as the engine. Invalid holiday structures fail before source requests. Risk history records additional closures as operator-supplied/unverified, refuses removed bundled closures and unknown special sessions, and preserves conflicting provider observations for downstream rejection.

All **552 Python, 58 dashboard and 13 Worker tests** pass locally, with two existing Starlette warnings. Required and changed-script Ruff passes. The local compiled-dashboard drill loads actual Python-published research reports and an imported company-event store, verifies all three authenticated exports, custom capital/position settings, halt/restart/watchlist persistence, missing configured-source failures and restoration. Unauthenticated exports return 401; missing selected sources return 503 while the workspace stays usable. All temporary processes were closed.

The expanded Linux verifier now takes its environment from resolved Compose and mounts the selected directives file. Its exact CI artifact must pass the populated, missing-source and restored-source phases in addition to the post-start heartbeat check. This remains synthetic deployment wiring evidence, with no real source/provider request, live order, target-host deployment or operator acceptance. [Deployment contract](DEPLOYMENT_WIRING.md).

## Protection coverage and entry-boundary follow-up

The combined suite now passes **586 Python, 61 dashboard and 13 Worker tests**; Ruff, TypeScript and production build pass. New failure/restart tests enforce stored stops, friction-aware entry geometry, preserved stop strength, exact instrument identity, legacy scope reconfiguration and persisted halts at the final broker boundary. A real paper-engine/compiled-dashboard fixture and browser check show missing/invalid protection separately from a healthy heartbeat, with a durable halt surviving restart and explicit fixture repair. [Precise scope and limits](PROTECTION_COVERAGE.md). The Linux container test includes these fault/restart/restoration phases. No actual target deployment, live-session observation or acceptance review was performed.

## Invalid ledger and unavailable valuation follow-up

The combined suite passes **627 Python, 64 dashboard and 13 Worker tests**, required/changed-script Ruff, TypeScript and the production build. An isolated baseline reproduction showed fractional quantity truncation, a malformed basis suppressing the entire protective read, and nonfinite cash entering a sell. The corrected paths preserve corrupt source records, reject invalid fills atomically and continue independent valid exits where cash is usable. Fault tests also exposed and fixed nonfinite buffered-tick callback/freshness failures and a future-mark gap at the protective tick-reader boundary.

The compiled private dashboard and real paper factory were exercised with synthetic invalid basis, engine restart and explicit restoration of the original fixture value. The workspace remained available, portfolio/risk totals were withheld, invalid minutes stayed as null curve gaps, and restoring values did not resume entries. A damaged minute excludes its day from qualification even with 374 other valid observations. No browser warnings/errors; temporary resources closed. The Linux verifier exercises these phases and requires a new heartbeat after the invalid-ledger restart. Revision-specific CI artifacts provide image-verification results. [Contract and remaining gates](LEDGER_INTEGRITY.md).

## Quote timestamp and ordering follow-up

The suite passes **651 Python, 64 dashboard and 13 Worker tests**, required/changed-script Ruff, TypeScript and the production build. Reproductions confirmed stale arrivals replacing prices, future quotes passing cadence, duplicate closed minutes and a corrupted cumulative-volume baseline. Ingress/read/aggregation boundaries now reject these conditions. Actual SDK binary equity/index packets preserve exchange timestamps and stale vetoes in three host timezones; unit conversion also covers repeated DST wall times.

The compiled dashboard and real paper fixture show per-process rejected-update counts, retained price/quantity, separate collector source and a working Atlas handoff with saved context. Mobile document/panel widths are 390/360px; no browser warnings/errors, temporary resources closed and no AI provider request. The Linux container verifier adds actual SDK parsing and a stream-rejection phase. [Exact semantics, verification and remaining source gaps](TICK_INTEGRITY.md).

## Recorded account benchmark comparison follow-up

The suite passes **652 Python, 72 dashboard and 13 Worker tests**, required/changed-script Ruff, TypeScript, production build and hosted static export. The real paper broker generates six dated fills and 31 close marks; an independent Decimal oracle verifies daily cash, fees and equity including closed losses. Tests reject missing/invalid data, preserve the account cutoff and earlier fills entering a 253-mark window, check relative-return/statistical conventions and prove a concurrent WAL fill cannot mix account versions.

The production-server drill verifies six private comparison exports, authentication, no-store responses and matching bounded saved Atlas context without a model request. Browser checks cover benchmark/window switches, daily pagination and historical closed holdings, selected export URL, Atlas submission, missing-date details and a two-segment account chart gap. Mobile document/panel widths are 390/360px; no browser warnings/errors were captured. The temporary server stopped. The Linux image verifier adds a separate synthetic account phase; inspect its exact revision artifact before claiming container acceptance. Source adjustments, total-return/corporate-action handling, sector/factor attribution, target-host operations and strategy effectiveness remain unqualified. [Contract](ACCOUNT_BENCHMARK.md).


## External broker account boundary and observation workspace

The paper execution adapter now reads the same ledger it writes. Explicit Kite/IBKR observations preserve instrument/account identity, unknown funds and fractional quantities; IBKR pagination cannot silently return page zero as the full book. Kite available funds are no longer called portfolio net liquidation. HTTPS redirects, malformed/duplicate/nonfinite JSON and oversized reads are rejected. [Migration and scope](BROKER_OBSERVATIONS.md).

Local verification: 684 Python tests, 76 UI tests and 13 Worker tests pass; required Ruff, TypeScript and production UI build pass. The new authenticated production smoke checks 14 synthetic orders / 26 executions, partial completion/cancellation, repeated-read changes, quantity mismatch, stale and wrong-account evidence, private export and saved Atlas summary. No broker/model calls are made by this fixture.

Desktop/mobile browser checks cover search, status filtering, pagination, linked execution detail, export wiring and saved Atlas context with an explicit missing-provider error. The 390px layout has a 390px document and 360px panel, with table overflow contained. A discovered filter/detail mismatch was corrected and rechecked: details disappear when the selected order leaves the filtered view. Console errors: none. The production test server is stopped after verification.

The Linux container verification adds a separate `broker-observation` dashboard phase against the actual images. Exact revision/image/run evidence is recorded after CI completes. Current engineering verification does not establish actual broker capture, persistent live order lifecycle, account/cash/position reconciliation, provider source integrity or launch qualification. The overall goal remains incomplete.


## Retained broker lifecycle history and selected review

The combined suite passes **693 Python, 82 dashboard and 13 Worker tests**, required/changed-script Ruff, TypeScript, private production build and hosted static export. New cases exercise restart and duplicate-writer retention, changing/day-boundary behavior, persistent missing records, cancellation field semantics, malformed/tampered evidence, one-snapshot WAL reads, selected Atlas isolation and actual schema 2 recovery. [Contract and bounds](BROKER_LIFECYCLE_HISTORY.md).

The compiled production dashboard reads six retained captures spanning partial completion, a regressed fill/missing execution, two empty observations and restored records. All six authenticated no-store exports match their selection. Current records return to 14 orders/27 executions and zero current lifecycle issues while retaining 87 preceding issue occurrences. These are repeated synthetic findings, not unique incidents or financial losses.

Desktop/mobile browser checks cover recent selection, numeric sequence loading, changed-field inspection, persistent findings on empty captures, return to current, bound export URLs, search and split execution details. Atlas submission from capture 1 saves only its preceding evidence and zero cumulative findings, excluding later captures and current portfolio/market data. With an empty provider key it saves the explicit setup error and makes no model call. Returning Atlas to current context is verified. Mobile viewport/document widths are 390px; both history/evidence panels are 360px with contained table overflow. No browser warnings/errors were captured. The temporary production server is stopped.

A separate CLI drill exercises the actual collector entry point with a fake GET transport and the module append/inspect commands in subprocesses: private 0600 journal creation, identical-capture deduplication and identical inspection pass. No actual broker session is used. The Linux verifier adds `broker-history`, a logically compared SQLite backup, and `broker-history-restored`; exact head/image results are recorded after CI completes. Actual source coverage, acknowledged OMS, off-host recovery and target-host/strategy acceptance remain open.

## Retained paper/replay comparison follow-up

The combined suite passes **707 Python, 88 dashboard and 13 Worker tests**, required/changed-script Ruff, TypeScript and production builds. New checks exercise actual harness/telemetry cash-flow arithmetic, retained run identity, original fill/fee prefixes after later backdated runs, failed/concurrent-run rejection, complete requested calendar coverage, missing/invalid/skewed observations (including microseconds), independent Node recomputation, hash-bound exports/Atlas and hosted omission. Actual schema 2 recovery captures committed WAL replay state and preserves the selected report. [Contract and scope](RUN_COMPARISON.md).

The synthetic production drill uses 70 regular-session minutes, two fills per account and deliberate size/price/fee differences. Its gapped report retains one invalid observation, one absent observation and one 15-second mismatch; only 67 minutes pair and aggregate metrics remain unavailable. Desktop/mobile checks cover chart mode, accessible marker selection, minute pagination/detail, search/side filtering, selected export wiring, saved Atlas context and return to the current workspace. Responsive verification caught and fixed a tooltip overflow; the final phone document/panel/chart widths are 390/360/324px. Provider setup errors remain visible; no real model or broker call is made by this drill.

The Linux verifier adds `run-comparison`, `run-comparison-restored` and a replay-ledger recovery checkpoint. Revision-specific CI/image artifacts determine their executed result. These checks qualify synthetic engineering behavior only. Same-strategy/OOS parallel replay, independent provider inputs, real session effectiveness, intended-host operations and complete benchmark parity remain unclosed.

## Mature observation-history follow-up

Local checks pass **714 Python, 92 dashboard and 13 Worker tests**, required/changed-script Ruff, TypeScript and the production build. The comparison regression adds 43,200 preceding synthetic records while retaining 69 selected observations, 70 expected minutes and three explicit gaps. A separate 52,200-row fixture verifies 30 distinct qualifying synthetic sessions, offset-duplicate rejection, valid session-close ordering, late invalid-day exclusion and malformed clock/date handling. Pre-window fills remain in the accounting reconstruction. [Contract and bounds](OBSERVATION_HISTORY.md).

The actual CLI and authenticated production API verify selection counts, new private report creation, changed-report rejection, selected Atlas context, invalid-current-history withholding and restoration. Browser checks show the history counts at desktop 1280px and mobile 390px without document overflow, exercise selected Atlas review/reset with the explicit missing-provider error, and verify the new unavailable-performance explanation appears for an invalid timestamp and disappears after restoration. The captured research comparison remains separately identified. Temporary browser tabs and the server are closed.

Linux container phases now repeat the mature-history export/Atlas and invalid-current-observation checks before and after replay-ledger recovery. Exact revision/image artifacts record the executed outcome. These synthetic checks do not qualify real observations, target-host response time/retention/soak, strategy effectiveness or overall release acceptance.

The first mature-history CI run caught an existing broker fixture crossing IST midnight: its synthetic executions were ten minutes old and belonged to the preceding daily book. The production inspector correctly reported the mismatch. The fixture now compresses its synthetic timeline into the capture day; four cases cover exact midnight, one second after, five minutes after and an ordinary session time. Production daily-book validation is unchanged.
