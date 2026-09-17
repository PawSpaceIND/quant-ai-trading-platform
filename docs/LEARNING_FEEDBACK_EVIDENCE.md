# Journal-bound learning evidence — review boundary

Base: `5bacb52337351c41fe782ac848edf1ae33647614`.
Branch / PR: `fix/learning-feedback-evidence` / #138.
This is a paper-only engineering change, not a profitable-learning claim or permission
to merge, deploy, activate a candidate, widen the watchlist or enable live orders.

## Reused production paths

The existing factory already calls `restore_from_journal`. That now binds its
attribution engine to the selected tenant's journal. Each later evidence-weighting
call refreshes from resolved entry outcomes. The legacy account-delta/latest-trace
callback requests the same refresh for a bound engine; it cannot assign a result to
whichever agents happened to speak when a position closed. Unbound offline engines
retain their explicit manual record API and do not import production history.

The existing daemon quality-report writer refreshes that projection, then calls the
existing probability-drift evaluator through a read-only observer. No duplicate
calibration calculation or dashboard is introduced. Two structured report fields,
`learning_feedback` and `probability_drift`, retain details in the JSON. The current UI
already renders the additional concise status lines in `limitations`; it does not
render the new structured blocks as a new chart or detailed audit page.

## Credit rule and persistence

Policy: `pramana.entry_supporter_credit.v1`.
Only a filled BUY entry with a finite resolved net result, aware decision/exit times,
order/decision/tenant identity and valid recorded specialist opinions is eligible.
An exit before the entry or after the evaluation time is not accepted. BUY/STRONG_BUY
supporters with strictly positive confidence share the actual journaled net outcome.
SELL/STRONG_SELL dissenters, NEUTRAL/AVOID and muted opinions get neither reward nor
penalty: the code does not invent their hypothetical alternative trade. The original
0.75–1.25 weight band and per-regime minimum of ten observations are unchanged.

`paper_specialist_feedback`, in the existing paper ledger, records canonical source
payloads, policy, first observation time and SHA-256. Tenant/decision and tenant/order
identities are unique; UPDATE/DELETE are blocked. Repeated refresh reconstructs the
same projection instead of adding observations twice. Changed or deleted previously
consumed source, duplicate entry identity or corrupt audit data refuses the adaptive
projection rather than overwriting its history. New malformed/incomplete rows are
explicitly skipped. A refusal clears adaptive weights to the original unscored default;
it is NOT a new automatic trading halt and never disables independent exits.

The journal is the source authority. This change does not independently attest to
broker fills, statutory costs, source truth, or earlier manually edited journal rows.
Hashes are not signatures against an administrator rewriting a database. A before/after
weight sequence is deterministic replay in exit-time order, not evidence that a
late-resolved historical outcome was known earlier. The audit records first-consumption
time; actual later weight uses carry the policy and aggregate source-basis digest in
the existing decision-evidence rationale. Higher influence is not proof of skill.

The generic SQLite backup includes the added audit table. Coordinated institutional
multi-store and off-host recovery remain separate release gates. No host database or
backup was touched during development.

## Honest probability-drift integration

Raw consensus confidence is NOT relabelled as `probability_positive_after_cost`.
The optional private `learning-monitor.json` file lives beside `decision-quality.json`.
Without it the report says `unconfigured` with no fabricated probability score. This
release does not install that file or create forecasts on the owner's behalf.

All fields are required:

| Field | Meaning |
| --- | --- |
| schema | `pramana.learning_monitor.v1` |
| tenant_id | Must match the running report tenant |
| journal_filename | One adjacent regular file using the existing ForecastOutcomeJournal schema; no traversal or absolute path |
| candidate_id | Exact declared candidate, not a display-name guess |
| model_artifact_sha256 | Exact pinned artifact digest |
| cost_policy_sha256 | Exact pinned cost-policy digest |
| reference_start | Aware timestamp beginning the fixed reference decision window |
| reference_end | Aware timestamp ending it; reference outcomes must already be resolved by this point |
| recent_start | Aware timestamp not before reference_end and before evaluation time |
| maximum_recent_age_seconds | Explicit positive operator recency budget, bounded to one year for parser safety |

The configuration must be a private regular unlinked file. The source is opened
read-only with SQLite query-only mode. Forecast and outcome digests, aware times,
model/cost bindings, finite arithmetic, nonnegative costs and the after-cost target
are verified. At most 10,000 resolved candidate rows are admitted per snapshot;
excess data refuses rather than silently sampling a misleading subset. The source
journal has no tenant column: the configuration is an operator assertion that the
file belongs to that tenant, not cryptographic proof of ownership or authenticity.

Reference and recent windows are time-ordered, with each reference outcome complete
before the recent period. Overlapping decision-to-resolution intervals are removed
per subject before scoring. Each window requires the existing 30-sample floor.
This avoids inflating sample counts with the same subject's overlapping horizon,
but does NOT establish cross-asset independence, effective sample size or statistical
significance. No claimed 'three independent bets' or arbitrary '10–20 observations'
is substituted for measured data.

The existing ForecastOutcomeJournal performance arithmetic computes Brier/ECE/net
expectancy; the existing evaluate_probability_drift applies its unchanged policy.
States are `unconfigured`, `insufficient_sample`, `refused`, `healthy`, `degraded`.
A healthy observation does not approve a candidate or demonstrate profitability.
Degraded/refused observations use the existing HIGH-priority notification sinks and
appear in the quality report. Identical conditions are suppressed within a running
process and IST day; restart may emit again. A report is written before alert delivery
is attempted. Actual external delivery/receipt must be verified on the target host.

## Verification and deliberately open acceptance

Tests use synthetic journals and existing factory/notification fixtures. No provider,
model, broker, production credentials, account policy or live-money state is used.
The first GitHub run stopped at two new lint errors before Python tests; those were
fixed without changing a CI rule. The first expanded local run had 148 passes and
one test fixture lacking its temporary database directory; the fixture was repaired.
The first full candidate run had 2,634 passes and one legacy CLI fixture failure: its
closed-entry helper omitted exit_at. The fixture now supplies the same closure timestamp
written by the resolver, preserving every assertion. Real incomplete rows remain excluded.
The initial remote connection loss left an isolated uncommitted first draft; it is
retained and is not the published source. The later worktree starts from the exact
native-GitHub branch; primary and peer checkouts are not reset or overwritten.

Completed run IDs, full baseline/candidate counts, source hashes, regression results
and guard-sensitivity records are recorded in the PR after actual execution. Do not
substitute older green CI or count unavailable test results as passing.

| Requirement | Scope after this patch |
| --- | --- |
| Supporter/dissenter credit | Implemented; synthetic evidence must pass and owner review remains |
| Idempotent outcome consumption and restart | Implemented with immutable audit and replay |
| Later decision uses changed weights | Existing weight path refreshed, policy/basis recorded |
| Existing calibration report | Reused, including NEUTRAL exclusion; not recreated |
| Probability drift observer/report/alert wiring | Implemented for a genuinely populated, configured forecast journal |
| Live production probability producer and artifact binding | Not activated or verified; no conversion of raw confidence |
| Candidate training, comparison, review, activation, rollback | Existing components; end-to-end controlled rollout remains open |
| Real improvement, model calibration, forward net edge | Requires authentic time-ordered observations; no guaranteed timeline |
| Qualified history, three risk gates, diversified 50 names, MCX | Separate existing workstreams; no changes in this PR |
| AWS rollout, account continuity, external alerts, human acceptance | Remain separate; no deployment performed |
