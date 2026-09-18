# Recorded feature store to training-only data

This is the next data-assembly component after #143. It reads the existing
`PointInTimeFeatureStore`, preserves its original selection logic, emits the
existing fitter's exact input format inside an auditable package, and can pass
that package to the unchanged fitter and shadow-model format. It is not a
provider ingest job, automatic learning service, model approval or deployment.

## Read contract

`ReadOnlyFeatureSource` does not invoke the base class's schema-writing constructor.
It opens an existing private owned single-link regular file with SQLite `mode=ro`
and `query_only=ON`. Missing databases are not created. All selection happens in
one explicit read transaction. An independently committed writer does not change
that transaction's snapshot. The selected path's device/inode and private-file
properties are rechecked before returning. Ordinary reads do not update source
records; SQLite WAL/SHM coordination is not claimed to be free of sidecar activity.

Primary SQLite documentation: https://www.sqlite.org/isolation.html and
https://www.sqlite.org/uri.html . This does not use immutable=1 or disable locks.
It is not a filesystem sandbox or a defense against privileged complete forgery.

## Operator-supplied plan

The exact schema `pramana.feature_training_plan.v1` includes partition=training,
dataset_id, cutoff, horizon_seconds, maximum_feature_age_seconds, cost_policy_id,
cost_policy_sha256, adjustment_policy_id, features, price, cost and decisions.

Every feature/price/cost rule declares exactly feature, source_id, schema_id,
max_age_seconds and category. Predictor names are unique numeric feature names.
The price rule must use MARKET and schema `price:<adjustment_policy_id>`; the cost
rule must use BROKER and schema `cost_fraction:<cost_policy_sha256>`. The cost is a
recorded return fraction, under the supplied policy, known at the decision; this
component does not calculate statutory rates, brokerage, slippage or funding.
The endpoint and reference price use the same explicit source/adjustment schema.
These identifiers are declarations, not independently verified economic semantics.

Decisions are a complete ordered list of row_id, subject and decision_at. Subjects
must identify the intended venue/instrument/currency consistently; the unscoped
feature store does not provide account or tenant authentication. The plan is
validated before extracting values. All requested rows must succeed or the whole
extraction refuses. There is no selection based on labels, silent removal of bad
rows, filling missing features with zero, or fallback to another provider.
This cannot prove that the operator chose the plan before inspecting outcomes.
Study registration and independent review remain necessary.

At each decision the existing feature selector supplies only observations known
then. The reference quote may precede the decision within its declared age bound;
its actual time is retained. The exit quote must have observed_at equal to the
exact decision-plus-horizon time. Of matching source/schema revisions, the earliest
available by the training cutoff is selected. A later nearby quote is not a
replacement. This is a recorded-reference-price return, not an executed trade or
independently authenticated fill. Outcome availability remains separate from the
event time; no historical availability timestamp is backfilled or invented.

Gross return is computed with Decimal from the recorded endpoint/reference prices.
The fitter's 24-decimal input precision is used with half-even rounding. If that
rounding changes the positive-after-recorded-cost event, extraction refuses.
The result is not asserted to include every real trading cost. The aggregate row
age is conservative and must satisfy every declared source age limit used by the
existing fitter. Old fundamental inputs outside these limits remain unsupported.

## Package and fitting

The package contains its plan, generated dataset, original selected observation
payloads, assembly time and digest. Validation loads those records into the real
in-memory feature-store implementation and reconstructs exactly the same dataset.
Changed rows, orphan/duplicate lineage, wrong source bindings and future package
times refuse, including when the package's outer digest has been recalculated.
Replay checks selected evidence consistency, not the completeness of an external
database or the authenticity of a source. Hashes are not signatures.

Fitting revalidates the package and current source grants, then calls the existing
`fit_candidate`. Its configuration binding includes the exact package digest and
original fitter configuration digest. The fitted bundle's dataset hash binds the
exact generated data bytes. The model remains a candidate without trading authority.
No parent model schema, training policy or production weighting behavior is changed.

Limits are 100,000 source observations, 2,000 requested rows, 16 predictor features,
500 KB plan, 2 MB generated fitter data and 12 MB package. These are engineering
bounds, not statistical adequacy claims. Building fewer than 30 rows is allowed for
inspection; fitting still independently requires its original row/class minimums.
Each same-subject outcome interval must be non-overlapping. Different subjects and
non-overlapping windows are not assumed statistically independent.

## Private commands after owner review

Use an isolated offline workspace, reviewed source store and grants, and a private
existing output directory. Never point the source argument at the trading ledger.
No command creates grants, fetches credentials, contacts a provider, resets a
journal, changes a watchlist, approves a model or submits an order.

```bash
python scripts/build_feature_training_data.py build \
  --source /private/work/feature-observations.db \
  --plan /private/work/training-plan.json --grants /private/work/reviewed-grants.json \
  --output /private/work/training-package.json
python scripts/build_feature_training_data.py fit \
  --package /private/work/training-package.json --grants /private/work/reviewed-grants.json \
  --output /private/work/candidate.json --run-id reviewed-run --candidate-id reviewed-candidate
```

These paths are examples, not files created on AWS. Both commands use the actual
clock and refuse a process with live-money mode enabled. Existing output is refused
before input reads. The original private-file reader and no-replacement publisher
are reused. Their crash boundary remains unchanged: interrupted hard-link publication
can leave complete multiply-linked bytes requiring inspection; a late I/O error can
leave an output despite a nonzero return. Inspect rather than blindly retry or delete.
Command output omits private vectors, grants, paths and arbitrary exception strings.

## Verification and remaining closure

Behavior tests use the actual feature store with explicitly synthetic records.
Integration tests exercise the actual fitter, shadow writer, repeat handling and
both command modes. The guard inventory enumerates every new explicit `_check`
site plus the transaction, endpoint-digest and CLI mode/output boundaries. The
existing copied-source harness requires passing controls, named mutant assertion
failures, no collection errors/skips and source restoration. This is targeted
regression evidence, not exhaustive Boolean or filesystem-interleaving coverage.

No qualified production feature store, ingest producer, real market/cost observation
set or fitted production candidate is installed by this PR. The old decision journal
does not retain each horizon's exact endpoint/availability/source evidence, so it
must not be silently relabelled as this store. Genuine ingestion, qualified costs,
chronological holdout/forward evaluation, cumulative trial registration, scheduling,
received alerts, authenticated approval/activation/rollback and actual effectiveness
remain separate. Research panels, watchlist/MCX, shared-risk release, recovery and
human/security gates remain on the original crosswalk. Owner merges and deploys.
