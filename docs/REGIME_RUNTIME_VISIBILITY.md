# Regime observation and actual bar-count visibility

## Original scope

Closes the implementation part of the item-6 addendum's runtime/dashboard visibility
and counter-meaning requirements. This is not a replacement for #141's off-hours
required-risk warmup or its separate lifecycle review. It does not change daemon.py,
history providers, aggregation, classification thresholds, the consensus floor,
risk policies, the watchlist, credentials or the journal/outcome resolver schema.

## Source finding, not the addendum's earlier hypothesis

In current main cd824a0449de9d05519d402b407bf3d62873742b,
SwarmMarketAnalysisPipeline._technical_metrics binds price_history_bars to
Decimal(len(closes)). It is an actual count, not a configured window size. Its
one-minute rolling window can legitimately stay at sixty while new observations
replace old ones. It is not the number of fifteen-minute candles used by classify.
The field is retained to preserve existing specialist behaviour and compatibility.

The existing classifier scores at most REGIME_LOOKBACK=40 bars per timeframe and
requires MIN_REGIME_BARS=20. The actual RegimeSummary.bars_used is the scored count,
or available count when insufficient. A context with 120 daily bars therefore has
120 available /40 scored; sixty one-minute bars can yield only four closed 15-minute
buckets. Tests use the actual aggregation/classification functions and distinguish
these three facts instead of changing any thresholds to force a label.

A controlled intraday reconstruction with a real wider-window fetch boundary keeps
the technical input at sixty while the aggregated count progresses from 19 to 20.
The former is insufficient_history; the latter synthetic uptrend classifies. This
is not a claim about today's actual retained tick coverage on AWS. The previously
quoted 0.32 score is not treated as a classifier-imposed ceiling, and no artificial
confidence-increase or forced-trade criterion is used to close this task.

## Implemented data path

MarketContext.metrics now includes selected timeframe and separate daily/intraday
labels, actual bars_used and bars_available. Those additional features follow the
existing decision record path without a database migration or changes to its resolver.

After the existing context is computed, the pipeline captures an immutable serialized
observation. It is keyed by the instrument's market/exchange/asset/currency/contract
and metadata identity, timestamped by the actual analysis cutoff, and bounded to
64 cached instruments. This is a display-cache bound, not a raised watchlist cap.
Older concurrent analysis cannot overwrite a newer timestamp. Capturing does not
change the returned context or model/risk input calculations.

PilotTelemetry only reads this existing snapshot. It performs no provider request,
reclassification or historical-database scan. Reads never block on its short-held
capture lock. A busy/missing/invalid/future observation is reported as such, without
fabricated counts and without turning a display issue into a new protection halt.

Each runtime.watchlist record has optional regimeContext with schema
pramana.regime_observation.v1. An observed record carries:

- observedAt and ageSeconds (observation age, not market-quote age);
- technicalTimeframe=1m and technicalBars;
- daily and intraday summaries: timeframe, label, barsUsed, barsAvailable, classified,
  and string Decimal trendStrength/volatilityRatio/rangeFraction;
- minimumBars, lookbackBars, selectedTimeframe, selectedLabel and selectionReason.

The Engine feed observations panel now renders this under Last analysed regime
context. It displays scored /available bar counts, technical 1m count, selected
context/reason and the timestamp. Its typed reader checks schema, real integer
counts, labels, classification/count agreement, selection priority and aware time.
The browser recomputes age rather than trusting a stored ageSeconds. Missing legacy
or unavailable data reads unknown, not zero bars or a guessed regime.

## Timing and restart boundaries

This is the latest computed analysis context, not necessarily an executed or accepted
decision. It is not a current trading signal, a probability forecast, proof of a
fresh tick or proof that portfolio risk gates are supplied. The panel is labelled
accordingly. Old context retains its original time; new heartbeats do not restamp it.
The cache is intentionally process-local. After restart, it remains not_observed
until actual analysis runs; off-hours risk-history warming alone does not fabricate
a regime decision. Persisted historical decision features remain available in the
existing journal but are not silently treated as newly analysed context.

## Executable verification

New Python requirements first failed on unchanged main as two named assertions:
per-timeframe features missing, and no runtime observation source. Both pass with
the implementation. The dedicated new test file exercises actual sync/async pipeline
methods, the real runtime SQLite payload, underlying classifier labels, a 60-minute
rolling technical count, real wider-window aggregation, daily absence/fallback,
cache bound/identity, malformed input, old/future timestamps and nonblocking reads.

The original test_timeframes_and_regime cases continue to test the classifier. No
classifier or consensus parameter is weakened. Seventeen copied-source Python guard
experiments reuse the existing Kite-history verification runner, requiring passing
controls, failing named assertions, zero errors/skips and restored unchanged source.
Inventory is executable in tests/test_regime_visibility_guards.py.

The TypeScript unit tests render the real React component and its EngineFeedStatus
integration, verify missing/contradictory inputs, and test eighteen copied-source
guard removals through the same executable test file. Working files remain unchanged;
child failures must be assertions, not module/syntax/type errors. No new dependencies,
CI exception or changed existing test assertion is introduced.

Initial new-fixture errors are retained: the synthetic volatility helper returned
six bars for a requested empty daily series; its count contract was corrected. The
first TypeScript check identified a missing NODE_ENV in the new child-test environment;
that fixture now explicitly uses test. Neither correction changed production logic.
Exact completed counts, commit and CI evidence are recorded on the PR, not guessed here.

## Remaining acceptance

Owner review and merge remain explicit. Deploy only the reviewed main via the existing
host process, preserving data, paper mode and protections. Confirm an actual running
symbol's technical count, daily/15m scored/available counts, labels and observation time
against its recorded analysis. Then confirm after restart/off-hours that unknown data
stays unknown until analysis, without claiming provider or risk readiness from this panel.
No AWS inspection, deployment, real provider call, paid inference or live order is part
of this PR. It does not close genuine model fitting/activation, research-host publication,
watchlist/MCX expansion, off-host recovery or the other workstreams' acceptance gates.
