# Completed paper-trade evidence

Order fills are not completed trades. This module groups one instrument's activity from a flat position back to flat as one episode, including scaled entries and partial exits. A later re-entry starts a new episode. Net episode P&L is sale proceeds minus purchase notionals minus every recorded cash-debit fee associated with the episode's orders. Execution spread/slippage already embedded in fill prices are not subtracted twice.

Open episodes, including realized partial exits and their fees, remain outside completed-trade metrics until the position is flat. Open-episode fees are disclosed separately. These statistics therefore have a different accounting boundary from total account P&L. Breakeven episodes are neither wins nor losses. With no completed episodes, average P&L and win rate are unavailable. With no observed losses, profit factor is unavailable with an explicit reason, not infinite proof of quality.

The calculation uses one SQLite read snapshot and requires matched internal paper reconciliation plus configured INR/India cash-equity/ETF scope. Invalid or nonchronological instrument fill timestamps reject the report. Source hashes cover the selected fills and cost rows. This is not an independent price/cost audit, signed source provenance, calibration study or proof of completeness.

## Generate an immutable review artifact

```sh
.venv/bin/python -m quant_ai.validation.trade_evidence \
  --database /path/to/ledger.sqlite --tenant india-paper \
  --output /path/to/new-trade-evidence.json
```

The command is read-only, refuses to overwrite an output and sets output permissions to 0600. The report contains source/report hashes, all completed episode order IDs, open-position counts and explicitly scoped metrics. A unavailable report exits 2; a computed report exits 0. Successful computation does not approve a strategy or trading launch.

The pilot engine refreshes its summary at startup and before entries, alongside reconciliation. A new fill suppresses the old summary until refreshed; repeated checks also account for corrected recorded fees without a new fill. Detailed episodes remain in CLI artifacts; bounded summary fields, source hash and timestamp flow into the dashboard and its evidence export. Full replay work is outside the one-second protection sweep, but its cost still scales with account history and needs target-host measurement.

## Review and launch meaning

Research shows fill count, completed episodes, open episodes, closed net P&L, sample mean P&L, win rate, profit factor and closed/open cash fees. Forward observation now requires at least 100 ledger-derived completed episodes as well as 30 qualified paper days and the release-bound strategy review. These are minimum filters, not adequate statistical evidence on their own.

Results are account-level. A reviewer must independently establish that the evaluated trades belong to the frozen strategy/configuration and intended data regime. Strategy/version attribution, AI/infrastructure cost allocation, holdout/walk-forward results, calibration, serial dependence, mark-to-market drawdown, liquidity/gap stress and real forward-feed provenance remain additional requirements. Do not equate this historical sample mean with a forecast edge or agent confidence with win probability. The signed review's claimed metrics must agree with retained episode evidence and the strategy-specific validation set.

Local verification used synthetic QA state with one open fill: zero completed trades, one open episode, no completed-trade mean or win-rate estimate. Separate tests verify scaled entries, partial exits, re-entry, fee-induced net losses, profit-factor semantics, invalid timestamps, unreconciled/unscoped accounts, ledger-version invalidation and fee-correction refresh. No new real trading or provider request was performed.
