# Offline numeric candidate fitting

## Scope

This is the training-only continuation of #139, using its `ShadowModelBundle`,
`TrainingDatasetManifest` and existing `execute_training` wrapper. It fits coefficients
from supplied numeric training rows; it does not retrain a hosted language model.
No provider, broker, daemon, approval, risk, watchlist or Research-reader code is changed.
The output has no trading authority and is directly consumable by the existing
`scripts/record_shadow_forecast.py --bundle ...` command after separate review.

## Operator input contract

Prepare an existing private directory (0700) and private single-link regular input
files (0600) owned by the invoking user. No real dataset or authenticated source grant
is installed by this change. Do not fabricate them or label a consumed holdout training
to make this command run. The command does not retrieve market data or read `.env`.

The dataset is UTF-8 JSON with exactly these top-level fields:

| Field | Contract |
|---|---|
| schema | `pramana.logistic_training_data.v1` |
| partition | `training`, never `holdout` or `forward` |
| dataset_id | A stable caller-supplied identifier |
| cutoff | A timezone-aware instant no later than actual fitting start |
| source_ids | Unique list of the sources actually used by the rows |
| feature_names | Ordered, unique numeric feature names; 1–16 |
| horizon_seconds | Positive integer, at most 86400 |
| maximum_feature_age_seconds | Positive integer, at most 86400 |
| cost_policy_id / cost_policy_sha256 | Declared version and 64-hex digest of the cost policy used in the supplied labels |
| adjustment_policy_id | Explicit declared preprocessing/price-adjustment policy |
| rows | 30–2000 supplied observations; never padded or resampled to meet this limit |

Each row has exactly these fields:

| Field | Contract |
|---|---|
| row_id / subject | Unique row identifier and full caller-declared subject identifier |
| decision_at | Timezone-aware prediction instant |
| observed_at / available_at | Feature observation and first-known times, both no later than decision |
| resolve_after | Exactly decision time plus the declared horizon |
| outcome_available_at | No earlier than horizon end and no later than the training cutoff |
| source_ids | Nonempty unique source subset, with permission for training |
| values | Exact feature-name mapping to finite decimal strings |
| gross_return / cost_fraction | Supplied fractional returns and total return-basis costs, both decimal strings |

The label is computed as `gross_return - cost_fraction > 0`. It is not inferred from
confidence, stance, a profitable gross move, or a take-profit trigger. Equal-to-zero
net outcomes are not positive. Money/return arithmetic is Decimal. Numeric operating
bounds reject gross returns outside [-1,10], costs outside [0,1], and net returns below
-1; these bounds are not statutory rates or invented transaction-cost assumptions.
Values are bounded to absolute 1e12, with decimal exponents from -24 through 12.

Rows must be chronological. Duplicate IDs/subject-times and overlapping outcome
windows for the same subject are refused. This does not establish independence across
subjects, non-overlapping observations, markets or successive days. At least five rows
of each net-outcome class are required. These minimums are engineering input checks,
not evidence of adequate statistical power or an economic edge.

A separate grants file contains a JSON list of existing `SourceGrant` contracts:
`source_id`, `provider`, `categories`, `planes`, `point_in_time`, `rights_status`,
`max_age_seconds` (optional). Categories and planes use existing enum names. Each
selected source needs TRAINING access, point-in-time declarations and INTERNAL or
VERIFIED rights; UNVERIFIED and decision-only/research-only sources refuse. The selected
grant identifiers must exactly match the dataset sources. More restrictive source age
limits are enforced. A JSON declaration is not an authenticated reviewer signature or
proof that the claimed provider/data rights/timestamps are correct.

## Command

Use the reviewed source checkout and a suitable Python environment:

```bash
TRADING_LIVE_MONEY_ACTIVE=false python scripts/fit_shadow_candidate.py   --input /private/reviewed/training.json   --grants /private/reviewed/source-grants.json   --output /private/reviewed/new-candidate.json   --run-id reviewed-run-identifier   --candidate-id reviewed-candidate-identifier
```

Those paths are illustrative, not files supplied with the build. The command uses the
actual UTC clock and exposes no timestamp override. Existing outputs are rejected
before fitting, and the command never overwrites an output, even under a competing
publication. A failure does not authorize discarding a previous output or retrying with
altered source/holdout labels. Standard output contains only a small TRAINING_ONLY
summary; raw vectors, grants, dataset paths and arbitrary exception text are omitted.

## Optimization and reproducibility

The numeric model minimizes average logistic cross-entropy plus `l2/2 * sum(w_j^2)`;
the intercept is unpenalized. Features are scaled using training-only max-absolute
values bounded below by one. The solver starts at zero, uses deterministic full-batch
gradients and an inverse curvature-trace bound for its step size. The Hessian is bounded
by one quarter of the mean augmented squared feature norm plus l2. No randomized
initialization, hyperparameter search or held-out selection occurs.

Defaults: l2=0.1, gradient-infinity tolerance=1e-6, maximum 512 iterations, Decimal
precision34 with ROUND_HALF_EVEN. API bounds prevent unbounded compute and overprecision;
rows × features × maximum iterations may not exceed20 million. Non-convergence or
invalid/increasing-over-initial objective causes refusal, not a falsely successful
candidate. The in-sample objective is an optimization diagnostic, not a Sharpe ratio,
out-of-sample score, calibration result or prediction of profits.

Training scales are folded into the exported raw-coordinate coefficients, so #139's
existing bounded numeric inference needs no new model schema or hidden serving scaler.
The source manifest hashes the exact input bytes. The training run binds dataset
metadata, model bytes, implementation modules and full optimizer/model/grant declarations.
Actual completion time, not the earlier start time, is recorded as trained_at. The
numeric API returns a fresh diagnostic view including configuration and training scales.
The minimal CLI writes the directly compatible bundle; retain the input, grants and
reviewed source to reproduce it. Local hashes are not signatures or universal
protection against privileged rewriting. Declared preprocessing and source authenticity
remain review obligations.

## Output publication and crash boundary

A same-directory private temporary file is written completely, fsynced and read back
before a no-replacement hard link publishes it. The temporary link is removed and the
directory fsynced. A competing existing destination is never replaced. A crash before
linking leaves no final output; a crash just after linking can leave a complete output
with two hard links. Existing private shadow readers reject that until an operator
reviews and removes only the verified orphan staging link. A late fsync error can leave
a complete output despite a nonzero command status, so inspect rather than retry blindly.
No universal filesystem-race or privileged same-user adversary guarantee is claimed.

## Verification and remaining release gates

Tests independently verify the analytic gradient using finite differences, a symmetric
stationary-point oracle, reversal of learned direction when labels reverse, scale folding,
intercept-only class probability, deterministic decimal contexts, actual trained-bundle
record/retry/restart through the unchanged shadow writer, and process death before/after
publication. All test datasets, grants, costs and learned coefficients are synthetic.
They are not an authenticated market dataset or an activated production strategy.

The explicit `_require` inventory has a named test for every new fitter guard; separate
copied-source cases cover convergence refusal and CLI privacy/publication guards.
Controls must pass, mutations must produce named assertion failures (not import,
collection, type or filesystem errors), copies are restored and working bytes checked.
Actual numerical totals and exact-head CI are recorded on the PR after execution.

This component has no holdout input or automatic fitting schedule. It does not independently
detect an operator relabelling a previously consumed holdout, accumulate the project-wide
trial count, create real feature/outcome sources, qualify costs, or approve an artifact.
Use the existing cumulative research register and candidate governance before comparative
evaluation. Genuine out-of-sample/forward evidence, repeatable source acquisition,
calibration/drift windows, authenticated review/activation/rollback and loaded-host
acceptance remain explicit follow-ups. Nothing here reports whole-platform100% closure.
