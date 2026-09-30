# Atlas reference review and bounded rolling evaluation

Source review: 2026-09-30, Atlas base `9e62e9f8ff3c80a3d671571221ae4e280498953e`.
This is a source comparison, not a runtime or trading-readiness certification.

| Reference and license | Relevant public pattern | Atlas source already provides | Remaining gap / action |
| --- | --- | --- | --- |
| [LEAN engine](https://www.quantconnect.com/docs/v2/writing-algorithms/key-concepts/algorithm-engine), [Apache-2.0 repository](https://github.com/QuantConnect/Lean) | Event-driven engine, separate universe and transaction helpers | Scanner, execution daemon, paper ledger, risk and freshness gates | PR263 stages a prior-session research shortlist API; daily runtime selection and subscription/protection-safe wiring remain unactivated and require separate reviewed integration. |
| [Microsoft Qlib](https://github.com/microsoft/qlib), MIT | Research workflow and rolling retraining to evaluate changing distributions | Point-in-time feature store, source-backed training packages, bounded fitter and single chronological probability holdout | Add repeated chronological probability holdouts with explicit windows and fold-specific training baselines. Implemented in this patch. |
| [Nautilus live concepts](https://nautilustrader.io/docs/latest/concepts/live/) and [backtesting](https://nautilustrader.io/docs/latest/concepts/backtesting/), [LGPL-3.0 repository](https://github.com/nautechsystems/nautilus_trader) | Deterministic simulation plus explicit reconciliation of transport, persistence and external activity | Paper ledger, decision/protection evidence, outbox and read-only funnel links | PR263 links exact orders to distinct evidence paths; linkage alone does not prove position/cash conservation or broker reconciliation. No new reconciliation claim here. |
| [Current FreqAI running docs](https://www.freqtrade.io/en/stable/freqai-running/), [Freqtrade GPL-v3 license](https://github.com/freqtrade/freqtrade/blob/develop/LICENSE) | Repeated training followed by later sliding test windows | Deterministic walk-forward replay with friction and embargo; single fitted-model holdout | Reuse only chronological evaluation ideas. This patch uses expanding training and non-overlapping test windows, not FreqAI's sliding implementation. No crypto pair logic, 24/7 calendar, exchange adapter or framework code is copied. |

The user's 2024.9 FreqAI link was checked against current stable documentation.
All references are design inputs; no dependency, code, dataset or model is imported.
Availability of open framework software does not confer rights to market data.
NSE decisions retain supplied point-in-time source grants, recorded costs and their own
session timestamps; the evaluator makes no exchange-calendar or data-entitlement inference.

## Implemented acceptance and use

`learning.rolling_evaluation.evaluate_rolling_forecasts` reuses the existing source-replay
validator, fitter, label-availability purge and after-cost probability metrics. Supply
2–12 ordered folds with exactly `training_cutoff`, `holdout_start`, `holdout_end` (aware
ISO timestamps). Training expands; holdouts are half-open, non-overlapping, and end no
later than the evaluation clock. Every fold must satisfy the existing training class
support and at least 30 held-out rows. Invalid/undersized folds refuse the complete
run, rather than selectively dropping a losing or difficult fold.

Earlier test observations may enter later training only once their outcomes were
available at that later cutoff. The report retains each fold's model, identities,
purged rows, predictions and metrics. Pooled Brier/log-loss/calibration bins count each
held-out observation once. Constant-half and training-prevalence baselines use each
fold's training population; better/worse/tied fold counts accompany pooled scores.
There is no best-model selection, promotion, deployment, scheduler or trading hook.

The existing private command supports a fold file:

```sh
python scripts/evaluate_forecast_candidate.py \
  --package /private/research/package.json --grants /private/research/grants.json \
  --folds /private/research/folds.json --output /private/research/new-report.json \
  --run-id frozen-research-plan --candidate-id recorded-candidate
```

Input files must meet the command's existing private-file checks. Outputs are new
private files and cannot overwrite an existing report. `--folds` is mutually exclusive
with `--training-cutoff`/`--holdout-start`; the single-holdout command remains supported.
No provider or broker calls occur. Run on a bounded source-backed research package,
not a live state dump; fold/model details stay private.

Synthetic tests demonstrate implementation behavior, not market skill. Acceptance
covers disjoint row counting, late-label purging, retained high-cost losses, outcome
leakage resistance, malformed/overlapping/future plans and private CLI publication.
Freeze boundaries, input universe and candidate specification before evaluating real
research evidence. Retrospective scores neither establish calibrated future probabilities
nor adjust for repeated research selection. Opportunity returns are not execution/P&L;
missing real costs and temporal/cross-subject dependence remain limitations.

The prospective next step is a frozen paper comparison of the staged dynamic shortlist
against the fixed list with reliable marks and unchanged execution gates. Do not infer
that either shortlist activation or this comparison has already happened. Reliability,
prospective comparison and independent reconciliation/stress evidence govern any later
live go/no-go; elapsed weeks alone do not.
