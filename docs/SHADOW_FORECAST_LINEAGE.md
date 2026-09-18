# Artifact-bound shadow probability production

This is a separate follow-up stacked on PR #138 at
`7fcfafde2055663062ded20f140b57b74589bb4f`. It does not change that reviewed branch.
It reuses the training manifests, forecast journal, performance calculator,
probability-drift observer and isolated verification harness already in the build.

## Scope and authority

`learning/shadow.py` can execute an already supplied numeric logistic artifact and
append its forecast, exact input vector and declared training lineage atomically.
The standalone `scripts/record_shadow_forecast.py` makes this callable without
installing a new daemon or changing any broker, risk, watchlist or execution path.

This is SHADOW ONLY: there is no broker handle, order method, model promotion,
registry approval or activation. It does not retrain the hosted language model,
fit coefficients, provide a default model, or turn raw consensus confidence into
a probability. A caller must supply a previously prepared artifact plus its
TrainingDatasetManifest and TrainingRunManifest. Model validity and usefulness
still require independent data/training/evaluation review.

## Model contract

The only supported artifact is UTF-8 JSON with exactly these fields:

- `schema`: `pramana.shadow_logistic.v1`
- `event`: `positive_long_return_after_cost`
- `feature_names`: 1–64 ordered unique numeric input names
- `coefficients`: one finite Decimal string per input, in that order
- `intercept`: a finite Decimal string
- `horizon_seconds`: an integer from 1 through 86400
- `maximum_feature_age_seconds`: an integer from 1 through 86400
- `cost_policy_id`, `cost_policy_sha256`: explicit external cost-policy identity

The duration/numeric/count bounds are implementation bounds, not suggested
trading settings, statutory rates, or thresholds for approving a model.
Prediction is `1 / (1 + exp(-(intercept + sum(coefficient * value))))` in a local
34-digit Decimal context. A logit outside [-60, 60] refuses instead of silently
clipping it into extreme confidence. No executable/pickle/plugin deserialization.
Inputs must already use the training feature definitions and preprocessing;
this module does not infer scaling, units, or transformations from feature names.

The declared model family is `decimal_logistic`. The artifact bytes must match
the training-run SHA-256; the run must bind the exact dataset-manifest digest.
The feature digest is produced by `feature_schema_digest(ordered_names)` and the
label digest by `label_schema_digest(model)`. These bind the ordered numeric names
and the explicit event/horizon/cost digest. They do not verify original dataset
bytes, feature semantics, the correctness of preprocessing or a fee schedule.

## Input and publication contract

A feature file uses `pramana.numeric_features.v1`, a subject, aware `observed_at`
and `available_at`, nonempty distinct `source_ids`, and an exact `values` mapping
of numeric strings. Its availability cannot precede its observation or exceed
the decision time. Observation age must meet the model's explicit freshness
budget. Training must precede the prediction; no result is published from a
future training run. Values, timestamps and source labels are retained, not
replaced by a checksum without the source payload.

The command uses its current OS clock for a new decision. There is no command-line
backdating option. The Python clock injection exists for controlled tests; these
are local consistency boundaries, not authenticated external-time attestation.
The output reports SHADOW and `trading_authorized: false` and does not print
feature contents, model coefficients, input paths or arbitrary exception messages.

An exact `(tenant, candidate, pair_id)` retry returns the original forecast.
Changing the input or model under a reused identity refuses. A candidate cannot
silently acquire different model/training bytes; use a new reviewed identity.
A single tenant is pinned in a dedicated new shadow journal. An old unscoped
forecast journal or trading ledger cannot silently be adopted. Changing the
selected path underneath an open writer refuses.

The existing forecast insertion checks were extracted into `_insert_forecast`
without changing their behaviour. The shadow writer includes that insertion and
model/input records in one BEGIN IMMEDIATE transaction. Immutable audit tables,
replay of the numeric calculation, identity/digest validation and explicit bounds
prevent incomplete or inconsistent local records being accepted as sourced forecasts.
Interruption and competing local connection tests cover the tested transitions.
This is not distributed fencing or detection of a coordinated rollback of all
local files. Hashes are not signatures; privileged complete forgery is outside
these checks. Reads/writes are capped at 10,000 forecasts and replay retained
lineage; throughput and rotation need operational assessment before unattended use.

## Existing drift monitor

For this producer's journal, add `require_shadow_lineage: true` to the existing
`pramana.learning_monitor.v1` configuration. The monitor then requires the local
tenant/model/input replay to validate before computing any score. It cannot fall
back to an unscoped journal when required lineage is missing. A shadow journal
with its marker present is checked even without the optional flag. Existing
legacy configurations remain compatible and retain their weaker operator-declared
ownership limitation. Removing an optional requirement from an operator config
is not prevented by these local checks; production configuration approval is separate.

The normal 30 eligible samples per reference/recent window, chronological split,
per-subject overlap exclusion, recency requirement and after-cost arithmetic checks
remain unchanged. New forecasts have no invented outcomes. The existing explicit
outcome resolver API must subsequently receive actual qualified outcome/cost data.
The standalone command does not source, qualify or resolve those outcomes.

## Operator usage after review

Do not run this against the operating ledger or make up a bundle just to get a
score. Prepare reviewed private bundle and feature files (regular, user-owned,
mode 0600, unaliased). The bundle payload is obtained from
`ShadowModelBundle(dataset_manifest, training_run_manifest, artifact_bytes).payload()`.
It contains the exact manifests and UTF-8 artifact text, not credentials.

```
python scripts/record_shadow_forecast.py --help
```

A recording invocation requires all of `--bundle`, `--features`, `--journal`,
`--tenant` and `--pair-id`; the journal must be a separate explicit destination.
The command refuses a non-false live-money environment. It does not load `.env`,
renew credentials, contact a provider, restart services or update a candidate stage.
No real bundle, forecast journal, monitor configuration or schedule is installed
by this PR. Operator invocation and intended-host acceptance remain separate.

## Verification and open gates

The tests use clearly synthetic coefficients and input/return observations.
A test trainer returns a synthetic artifact through the actual training wrapper;
that is not evidence of a fitted production model. Numerical inference, source
binding, time refusal, private files, idempotence, concurrency, interrupted writes,
append-only records and the existing monitor integration are exercised.
The existing control/mutant harness verifies twelve named new boundary scenarios;
its inventory is executable in `tests/test_shadow_guard_sensitivity.py`.
Full-suite, lint and exact-head CI results are recorded on the follow-up PR after
completion. No lower sample floor, risk threshold, scanner exception or old test
assertion is permitted to substitute for a pass.

Still required: qualified dataset/features/costs; actual model fitting and frozen
out-of-sample evaluation; authenticated model/release review; a production feature
producer and real outcome source; host deployment/scheduling, alert receipt,
rotation/capacity and rollback; forward evidence of effectiveness. #133 owns
risk/history qualification; #136/#137 own execution/shared-risk continuation.
This component does not complete those milestones or guarantee an edge.

## Additional prepublication finding

A synthetic clock callback could mutate the caller-owned feature mapping and
model bundle between initial capture and inference. The new regression failed
with 0.880797... where the original supplied model/input required 0.5. The writer
now reconstructs the validated bundle and copies the input before invoking the
clock, then rechecks its selected journal path. The unchanged test passes with
0.5 and replayable lineage. This was an unpublished-candidate defect, not a
production incident. The paired sensitivity case covers removal of that snapshot.
