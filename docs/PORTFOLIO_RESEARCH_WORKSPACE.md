# Continuous portfolio research workspace

The authenticated Research page now displays a published replay of the separate continuous portfolio journal. Each candidate has independent cash and holdings on the shared supplied quote timeline. This is a simulated research record, distinct from independent-case lab totals and from the running paper account. Publication never accepts a strategy or changes readiness checks.

## Publish a private snapshot

Create and populate the journal with the [research extension workflow](research-provider-portfolios.md), then run from the repository:

```sh
PYTHONPATH=src .venv/bin/python -m quant_ai.research.portfolio_workspace \
  /private/research/portfolio.sqlite --name 'Frozen portfolio experiment 001' \
  --tenant YOUR_TENANT --output /private/research/portfolio-001.json
```

Set `PRAMANA_PORTFOLIO_RESEARCH_REPORT` to that new output path in the private workspace environment and restart the workspace. The tenant must match `PRAMANA_TENANT_ID`. Choose a new filename for every publication; the command refuses overwrites and writes with mode 0600. The reader never creates a missing database. Its SQLite connection is read-only and obtains a consistent transaction snapshot of configuration and events. It checks event digest, ID and contiguous sequence before replaying the same simulation engine.

The publisher accepts at most 16 candidates, 50 symbols, 5,000 events and a 4 MB envelope. Larger histories fail visibly; there is no silent truncation. This initial bounded interface is not a high-frequency research service. Publication is an offline snapshot, not a live-feed subscription or an automatic refresh schedule.

## Inspect and export

The page provides a candidate comparison table, candidate and equity/drawdown selectors, event-ordered curves with timestamp tooltips, current cash/equity/returns, realized and unrealized P&L, trading fees, drawdown, open holdings, pending orders, fills and cancelled remainders. Tables paginate after 25 rows. Candidate changes reset their pages. The fixed schema preserves every curve point within the publication limits.

Stale holdings cause current equity, return, unrealized P&L and current drawdown to be unavailable. The chart leaves valuation gaps disconnected. The maximum *observed* drawdown is a lower bound on the maximum that could have occurred during missing observations. A cash-only candidate can remain valued while another candidate has stale holdings. Displayed simulation halts block further simulated buys while allowing exits; they do not prove a live protective order or cap losses at the configured threshold.

`GET /api/research/portfolio` downloads the validated report as a private, no-store JSON attachment. The endpoint uses the existing session proxy, takes no caller-selected path and returns 404 when unconfigured or 503 when configured evidence is invalid. Missing, malformed and cross-tenant evidence remain unavailable; other workspace panels and readiness checks continue to operate.

Atlas receives the report identity, source/code hashes, assumptions, summary metrics and holdings, plus explicit counts for omitted curve and order detail. It does not receive the full curve, fill or order arrays through this context. The handoff prefills the selected candidate and does not automatically send a provider request. Questions and this bounded workspace context follow the existing configured-provider consent notice and persistence behavior.

The source cloud publisher omits `researchPortfolio`; the Worker independently removes it before storage. The hosted snapshot viewer has neither this report nor the private export endpoint. Raw quote provenance, decision references and arbitrary configuration are excluded from the workspace report. Keep raw research evidence private.

## Evidence and limits

The report retains a SHA-256 of canonical source configuration/events and a source fingerprint for the replay, publisher and validation helpers, with Python version. The envelope hash detects accidental/inconsistent changes; the UI checks structure, tenant, dates, current/gap metrics and absence of unsupported fields. These self-contained hashes do not authenticate a person, market source or model. Coordinated rewriting or deletion of a journal tail requires comparison with an independently retained trusted hash to detect. Archive the private source snapshot and code with that hash; the sanitized report cannot reconstruct omitted source details.

The engine models later-quote IOC fills, supplied side liquidity, partial fills, fees, slippage, cash/exposure limits and stale marks. Session state, instrument metadata, price adjustments and liquidity are supplied inputs requiring independent qualification. It does not process corporate actions, simulate queue priority or market impact, or include model, infrastructure and data costs. No positive expectancy or full vendor parity is established.

[Recovery bundle schema 2](RECOVERY_BUNDLE.md) now supports explicitly selected experiment, portfolio and company-event databases, provider receipt directories, published reports and raw input archives. It checks stored state and deterministic research reports against the retained manifest. Every source must be listed; it does not discover journals automatically. Verify encrypted off-host restoration and replay on the target host before closing recovery. A local export or synthetic recovery drill does not satisfy that gate.

## Local verification — 14 September 2026

The combined suite passes 485 Python tests, 33 UI tests and 13 Worker tests, with Ruff, TypeScript and a webpack production build. New tests cover read-only/no-create behavior, source digest/sequence/config changes, independent fee/cash/partial-fill calculations, expiration, stale gaps, gap drawdown and permitted exits, strict consumer validation, bounded persisted Atlas context and exclusion from cloud uploads/storage.

A synthetic 71-event browser/API drill contained 34 quote events, 35 order events, 32 fills, four missing valuations, an entry halt, one pending order and three cancellations. Desktop 1280px and mobile 390px layouts fit their viewports; both retained curve gaps. Candidate/chart switches, fill pagination, a downloaded attachment and Atlas prefill/close passed. A flat cash curve's axis initially repeated compact labels; it was corrected and rechecked with distinct 998–1,002 ticks. The final browser console contained no warnings/errors.

Unauthenticated workspace/export returned 401. Authenticated export matched the workspace report. Damaging the report returned 503 for export while workspace remained 200, readiness checks unchanged. Synthetic source evidence SHA-256: `122945bcfc1f22b1f003f5e0711ec57842c7ab3ff74185af8f24045196f576a3`; replay/publisher source fingerprint: `a4dd2c1fdef91ad7434cca58b4eefde4859814b367711e247b0fb2f63d7bb2b9`.

All current verification used isolated synthetic stores and an empty provider key. No paid API request, market-source fetch, real broker order, acceptance signature or deployment occurred. The temporary server and browser tab were stopped/closed. Real prospective replay qualification, company-event dashboard integration, paired-provider performance and target-host operations remain open. The subsequent schema-2 recovery implementation closes the local selected-research capture/restore gap, with external recovery qualification still required.
