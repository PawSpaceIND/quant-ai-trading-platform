# Strategy-linked paper evidence

An account's trade count does not prove that one strategy has been evaluated. Research now shows account totals and exact-configuration episode evidence separately. Forward acceptance uses the latter, with matching observation days and a signed review of the same evidence snapshot.

## Episode ownership and coverage

A completed trade remains one flat-to-flat instrument episode, including every scaled entry and partial exit. To link it to a configuration, every fill must have exactly one canonical ledger proof, matching tenant, order, instrument, quantity, fill price/time and recorded cash fees. Swarm proofs must also match the approved order. A protective exit carries its own freshly captured runtime binding; it does not inherit the entry's identity by assumption.

Each binding must be `matched`, with equal startup/current hashes, no issues, a check within 10 seconds of the fill (up to five seconds clock lead), and a source-check age at most 65 seconds. The referenced tenant-specific manifest must have an intact canonical checksum, matching source inventory hash, release and paper mode, and must have been recorded before the fill within that clock tolerance. Missing, conflicting, stale, altered or cross-configuration proofs cannot produce a linked episode.

Mixed and unproven episodes remain visible in account P&L. They are not silently omitted from approval: an unresolved episode touching a configuration, **or any unlinked episode closing after that configuration was first recorded**, blocks its acceptance. Unproven open episodes also block it. This conservative coverage period prevents a wholly missing pair of proofs from hiding a losing trade. A legacy episode completed before the configuration's first record is disclosed in account totals but does not automatically contaminate a later evaluation. Known foreign open positions prevent qualifying observations until resolved.

A new fill, changed fees/proofs, or changed manifest record changes the source/evidence fingerprint. A fee correction that no longer matches a fill's canonical proof remains unresolved; the tool does not rewrite historical evidence or create a fabricated correction trail. Reported linked statistics can still be inspected while coverage is incomplete, but cannot pass acceptance.

## Configuration-qualified days

Valuations now record the current configuration and whether its position/evidence ownership was verified. A qualifying past day needs at least 300 **distinct** eligible fresh minute samples, including the final five minutes of the NSE cash session. The count starts at the configuration's first recorded time. Legacy samples, other configurations, incompatible fill dates, stale/missing ownership and foreign open episodes do not supply qualifying samples. A mixed-configuration qualifying session is excluded.

These are configuration-matched observations, not an independent proof of licensed market data, continuous uptime, complete AI inputs, or a strategy return attribution model. Account Sharpe/Sortino remain labeled account-return measures. They are not relabeled as strategy-specific risk-adjusted performance.

## Export and review

```sh
python -m quant_ai.validation.trade_evidence \
  --database /data/pramana.db --tenant ghost \
  --strategy-sha256 FULL_RUNTIME_MANIFEST_HASH \
  --output /data/evidence/strategy-episodes-001.json
```

The read-only export refuses overwrite, writes mode 0600 and retains account episodes, attribution status/reasons, exact order groups and the selected configuration report. Use `selectedStrategy.evidenceSha256` for `strategy_evidence_sha256` in the operator review artifact. `strategy_config_sha256` remains the runtime manifest hash. Exporting a report signs nothing and approves nothing.

The dashboard requires an intact fresh runtime manifest and current-ledger strategy evidence with zero unresolved or foreign open episodes. The signed review must match the evidence fingerprint, completed-trade count, after-fee mean P&L and profit factor. It still requires at least 100 linked completed episodes, positive historical mean, profit factor at least 1.2 and at least 30 configuration-qualified days; claimed paper days cannot exceed recorded days. Undefined metrics remain unavailable. Existing AI holdout, cost, trial, calibration, regime, drawdown, release, signature and expiry requirements remain applicable through the review workflow.

Any subsequent evidence change requires a new review of the new snapshot. Never copy a matching hash around to manufacture acceptance. No review enables live-money orders.

## Operational limits

Evidence construction scans account history and canonical proofs in one SQLite read snapshot. Summaries are refreshed at startup, before entries, after protective sweeps containing fills and at the end of a governed cadence. Protective liquidations in a sweep finish before the full report is rebuilt. The one-second telemetry path caches the selected report between changes and omits large order-ID lists; CLI artifacts retain them. Source checks for a protective fill happen before that fill, but a metadata-capture error cannot suppress liquidation and is recorded as unavailable.

Large-account latency and target-host behavior still need measurement. Configuration linkage does not establish economic causality, benchmark/factor attribution, genuine forward data, AI calibration, stable model weights, optimal parameters or profitability. Corporate actions, liquidity, gap/latency stress, infrastructure/AI costs, AI-specific unseen-data evaluation and independent operator qualification remain separate work.
