# Recorded paper versus retained replay

The private Research page can compare recorded paper account observations with one retained historical harness run over an explicitly selected window. This advances the equity/fill inspection part of [QuantConnect's documented reconciliation workflow](https://www.quantconnect.com/docs/v2/cloud-platform/live-trading/reconciliation). QuantConnect compares a live deployment with a parallel out-of-sample backtest. This implementation is an **unqualified paper/replay diagnostic**: it does not establish equivalent strategy, scheduling, provider inputs, initial state, execution conditions or out-of-sample independence.

## Retained runs and publication

Each new `HistoricalReplayHarness.run()` receives a unique `replay_run_id`, also returned in its tear sheet. The broker database retains source-file inventory, dataset/configuration hashes, component descriptions, execution assumptions, account boundaries and per-step valuations. A later replay may replace the legacy latest-per-timestamp projection but cannot overwrite a selected run's retained history. The completed run binds its original fill/fee prefix and recorded account state. Only one running harness is allowed per replay account. An interrupted running record requires operator review; use a new isolated replay account instead of silently reusing it. Failed, interrupted and source-changing runs remain distinguishable and cannot publish a completed comparison. Existing historical databases without retained runs are not backfilled with invented provenance.

Use a separate replay account/database so research does not change the observed paper account. Supply an actual returned run ID and new report path:

```sh
python -m quant_ai.validation.run_comparison \
  --paper-database /data/pramana.db --paper-tenant india-paper \
  --replay-database /data/research/replay.sqlite --replay-tenant replay \
  --replay-run RETURNED_RUN_ID \
  --start 2026-09-11T03:45:00Z --end 2026-09-11T10:00:00Z \
  --max-skew-seconds 0 --output /data/research/run-comparison-001.json
```

This reads each source in its own consistent SQLite transaction. It writes a new mode-0600 report and refuses to replace an existing output. No model, broker or market-provider call is made by the comparison command. `PRAMANA_RUN_COMPARISON` selects the report in the private dashboard and is forwarded by Compose. The report is account-bound and excluded by both the hosted snapshot publisher and Worker ingestion.

Paper capture selects the requested session grid even when the account has more than 10,000 retained observations. It includes all recorded fills/fees needed for historical balance reconstruction, loads only referenced strategy manifests and discloses selected/retained/excluded observation counts in the report, Source and method and Atlas context. Invalid selected records remain gaps or rejection; unlocatable stored timestamps fail capture. [History bounds, qualification readers and verification](OBSERVATION_HISTORY.md).

## Clock, accounting and interpretation

The requested range is half-open: start included, end excluded; both boundaries must be minute-aligned and in the past. The current calendar scope is regular NSE cash sessions in 2026, including the explicitly documented Budget Sunday. Window size is at most 45 calendar days and 10,000 expected session minutes. All expected minutes remain represented. Missing, invalid, duplicate or mismatched evidence is not forward-filled or silently dropped. Observation skew defaults to zero; an explicitly chosen 0–59 second tolerance is disclosed and does not repair missing data.

Paper cash/positions/fills/fees are independently reconciled before capture. Historical points reconstruct cash and weighted entry basis from the corresponding fill prefix, require exact quantities and check the recorded marks/equity. Paper held marks require an actual live-tick source and age of at most 120 seconds at that observation. Missing/invalid marks leave gaps. INR cash equity/ETF scope, nonnegative cash and unsupported account flows remain explicit constraints. Retained hashes demonstrate consistency with stored evidence, not source authenticity.

Whole-window returns use each run's own first requested observation. They and aggregate divergence statistics are withheld unless every expected minute has two valid observations within tolerance and at least two minutes exist. Different starting books are disclosed, not declared equivalent or normalized away. The chart offers INR equity and rebasing to 100; a missing initial value withholds the respective rebased series. Different observation times are not simultaneous market prices.

Fills are grouped by minute, instrument and side. Quantity, weighted average price, recorded cash fees and differences are inspectable. Chart markers sit on the equity curve in the fill's minute, not an execution-price axis; up to 200 markers with available equity are shown. Every selected fill remains in the export. Grouping is not one-to-one order matching or a causal attribution model. Spread/slippage already enter execution prices and are not deducted again as cash fees.

The retained replay run records the runtime configuration it ran under and the decision-maker that produced it; a replay curve comes from the deterministic consensus, a paper curve from the live decision-maker, so the two are not the same agent. [What a replay tests and what it cannot](BACKTEST_FIDELITY.md). Source inventory and recorded components are compared when a paper strategy manifest is available. Even identical inventories/components do not prove provider equivalence, scheduler/clock parity, identical AI responses, point-in-time availability or untouched holdout status. `strategyEquivalence` remains `unverified` and `automaticPromotion` remains false.

## Private UI, AI and recovery

Research includes curves, fill markers, minute pagination/selection, missing-minute details, instrument/side filtering, private export and Atlas handoff. Export/Atlas bind the selected report hash; a changed, missing or corrupted report invalidates that selection. Atlas receives only a bounded historical summary, excluding current portfolio/market state, earlier conversations, account identifiers and raw orders. The provider remains separately configured; an absent key saves a visible error and the inspected context.

For schema 2 recovery, explicitly select the replay database as `kind: replay_ledger` and the published report/input datasets as `file` or `directory` entries. The replay database kind verifies integrity, all stored rows, run hashes and completed INR/NSE cash source captures. Unfinished/failed runs remain unqualified. SQLite backup includes committed WAL state; do not copy a live SQLite main file as an ordinary file. See [recovery contract](RECOVERY_BUNDLE.md). Full deployed inventory and encrypted off-host restoration remain external acceptance gates.

## Evidence and open scope

The synthetic drill runs the actual historical harness and paper broker/telemetry over 70 minutes. It deliberately changes paper size (8 versus 10 shares), execution prices and recorded fees; this is not independent live AI performance. An independent cash-flow oracle verifies ending balances and a roughly −₹6.04 paper-minus-replay difference. The gap fixture injects one invalid minute, one missing minute and a 15-second observation mismatch: 67 of 70 minutes pair and all whole-window metrics are withheld.

Tests also cover changed fills/fees/metadata, failed runs, retained earlier runs after another backdated replay, atomic run-point writes, calendar boundaries, report tampering, selected API/Atlas isolation, hosted exclusion and actual bundle/WAL recovery. Revision-specific local browser and Linux image results are recorded separately. Same-strategy parallel replay, complete provider inputs, real session evidence, broker execution reconciliation and the overall pilot acceptance remain open. This report cannot certify returns or prevent all losses.
