# Chronological forecast evaluation

Atlas can now fit a candidate on earlier recorded observations and measure its
predictions on a later holdout. This connects the existing source-backed data
assembler, logistic fitter, bounded shadow-model arithmetic and probability
scoring. It does not change the pilot consensus policy or authorize a trade.

The specialist scores in the pilot are not automatically calibrated probabilities.
This evaluator tests a separately fitted candidate against two declared baselines:
a constant 0.5 probability and the positive-after-recorded-cost rate in training.
The baseline rate never uses holdout outcomes. Positive Brier/log-loss improvement
means smaller error than that baseline on this particular sample; it is not an
approval decision or a claim of future profit.

## Inputs and split

Use the existing `build_feature_training_data.py build` command to make a reviewed
recorded-observation package. Its existing schema and `partition=training` field
are unchanged. In this command that package is the complete research input:
**only the earlier subset is fitted**, under a new dataset identity and digest.
Do not run the ordinary `fit` command on the whole package when reserving a holdout.
No old decision journal is upgraded into point-in-time observations by inference.

Choose the feature set, sample, costs, training cutoff and holdout start before
examining the test results. The evaluator replays the package's recorded feature,
price and cost lineage before fitting. All supplied rows are accounted for:

- Decision at or before training cutoff, label available by cutoff: training.
- Decision at or before cutoff, label unavailable then: excluded with that reason.
- Decision after cutoff but before holdout start: excluded as the embargo interval.
- Decision at or after holdout start: holdout. Every such row is scored or the
  evaluation fails; failed predictions and unfamiliar model inputs are not dropped.

Training cutoff must strictly precede holdout start. Training labels must have
resolved and become available by cutoff. This separates training outcomes from
the test interval across all subjects. The original data assembler additionally
refuses overlapping outcome windows for the same subject and future/stale features.
Different instruments and adjacent observations can still be statistically dependent.

Training retains the fitter's minimum of 30 rows and five observations per class.
Holdout requires at least 30 rows and may have only one outcome class. These are
engineering minimums, not evidence of statistical significance. No threshold or
hyperparameter search is performed on the holdout.

## Run locally

Use existing private input files and an existing private output directory:

```bash
TRADING_LIVE_MONEY_ACTIVE=false python scripts/evaluate_forecast_candidate.py \
  --package /private/work/research-package.json \
  --grants /private/work/reviewed-grants.json \
  --training-cutoff 2026-08-01T00:00:00+00:00 \
  --holdout-start 2026-08-03T00:00:00+00:00 \
  --run-id reviewed-evaluation --candidate-id reviewed-candidate \
  --output /private/work/new-evaluation.json
```

These are illustrative dates and paths, not installed production data. The command
uses the actual wall clock and refuses live-money mode. It reuses the existing
private-file reader and atomic no-replacement publisher. Existing outputs are
refused before reading inputs; private paths, row values and arbitrary errors are
not printed. As with the existing publisher, a late filesystem failure can leave a
complete output: inspect it before retrying. The full report is private and
contains instrument identities and per-row results.

## Evidence retained

The report includes the input package digest, explicit partition row IDs and
exclusion reasons, fitted model bundle, training diagnostics and timestamps,
evaluator code digest, per-row probabilities and outcomes, probability calibration
bins, Brier score, log loss and expected calibration error for model and baselines.
A digest binds the report. These hashes provide consistency, not source authenticity.

Training identities, scaling, coefficients and baseline probabilities depend only
on training records. Changing only holdout outcomes must leave the fitted model
and its predictions unchanged. A synthetic test reverses the test-period relation
and requires the report to show deterioration instead of silently fitting it away.

The model is trained **now**. Retrospective arithmetic does not backdate that event,
write old predictions into a forward journal, or disable the shadow writer's
model-time checks. The report explicitly says `RETROSPECTIVE_HOLDOUT` and
`forward_paper_evaluated=false`. Evaluating calibration does not certify it:
`calibration_verified` and trading/promotion authority remain false.

## What this does not prove

The feature set, rows and split are supplied; the tool cannot prove they were chosen
before anyone saw the outcomes. One holdout does not correct for repeated trials,
establish regime stability or replace a registered walk-forward/forward-paper study.
Retain every attempted study and keep a fresh final test period for later selection.
Recorded cost fractions may omit real brokerage, slippage, funding or taxes.
Outcome returns describe opportunities, not executed trades or portfolio P&L.

Next integration steps are qualified real observations, registered repeated-window
evaluation, forward shadow outcomes, and cost/risk-aware selection using qualified
forecasts. This PR alone neither trains a production model nor demonstrates an
improvement in Atlas's live decisions.
