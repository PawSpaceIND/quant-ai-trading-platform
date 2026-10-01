# Offline fixed-versus-dynamic PAPER research comparison

`quant_ai.operations.paper_profile_comparison.compare_paper_profiles` closes a
specific evidence gap: comparing recorded fixed-list and dynamic-list research
cohorts under one frozen strategy/risk/cost declaration. It does not run a strategy,
activate a watchlist, fit/train a model, open a database, fetch history, invoke a
provider or create orders. It reuses the existing forecast evaluator's after-cost
Brier/log-loss/calibration-bin arithmetic and constant-half baseline.

This is research cohort evaluation, not a simulated portfolio or a claim of
increased returns. The selected cohorts can differ. Their mean opportunity returns
must not be compared as paired trading skill or added into portfolio P&L. Only
common opportunities with both recorded probabilities and a resolved shared
outcome enter the matched forecast comparison. No winner/significance/promotion
verdict is produced; samples and an explicit descriptive 30-pair count flag remain
visible. Elapsed time or more observations do not authorize live trading.

## Frozen contract and inputs

Pass JSON-compatible dictionaries/lists and a timezone-aware `now`. The strict
`pramana.paper_profile_plan.v1` plan contains exactly:

- `mode=PAPER`, `venue=NSE_CASH`, `event=positive_long_return_after_cost`;
- `frozen_at < start < end`, a half-open decision window no longer than 90 days;
- `catalog`: distinct caller-declared eligible symbols, bounded to 50;
- `fixed_symbols`: nonempty distinct subset of that catalog;
- `dynamic_limit`: integer 1–15; smaller or empty actual selections are valid;
- `horizon_seconds`: one fixed integer horizon, at most 30 days;
- `strategy_sha256`, `risk_policy_sha256`, `cost_policy_sha256`,
  `dynamic_rule_sha256`: reviewed 64-hex declarations. These are common fingerprints,
  not permission to modify or proof of actual runtime settings.

Each recorded cycle contains its unique ID, strictly increasing `decision_at`,
exact `plan_sha256` (SHA256 of existing canonical JSON), `selection_cutoff`,
`selection_available_at`, `selection_sha256`, `dynamic_selected` and complete
`opportunities` for every catalog symbol. Missing subjects or repeated cycle/
subject boundaries refuse rather than improving a denominator. Cutoff must precede
that Indian decision date, and selection must have been available before the
recorded decision. No new scanner or rule for buying previous winners is introduced.
The source hash binds the declaration; this API cannot independently authenticate
selection freshness, licensed data, source eligibility, scheduler completeness or
an actual prospective freeze. Those flags remain false. Preserve existing runtime
freshness/subscription/quorum/protection gates for any separately approved experiment.

Every opportunity contains `symbol`, `input_available_at` (or null),
`proof_sha256`, exact `resolve_after`, `outcome` (or null), and both `profiles`.
The two profile records each contain `status`, `probability` (numeric string or
null), `forecast_available_at` (or null), and `reasons` (bounded unique codes).
Statuses are UNSELECTED/HOLD/REJECTION/PROPOSAL; they must match the recorded
fixed/dynamic membership. HOLD/REJECTION needs an explicit reason. Known-empty
PROPOSAL reasons stay empty. Unselected symbols cannot acquire forecasts, and
unknown inputs cannot support a forecast/proposal. Forecasts and inputs must have
been available by the decision time. Confidence is not converted into probability.

A complete outcome contains `resolved_at >= resolve_after`, `gross_return`,
nonnegative `cost_return`, and `evidence_sha256`. Numeric values are finite strings,
and resolution cannot be future to `now`. The same realized long-opportunity
outcome/horizon/cost observation is used for both forecasts: label positive only
when gross minus cost exceeds zero. Unknown/partial outcomes refuse or stay null;
a missing outcome never becomes a zero or negative label. Missing forecasts and
pending opportunities stay in denominator fields. Separate selected, resolved,
unresolved, scored and resolved-without-forecast counts expose attrition. Gate codes
can retain stale/quorum/budget/strategy reasons; this API does not invent missing
reason attribution.

## Existing Claude/OpenAI comparison

The existing `ChallengerConsensusClient` schedules Claude primary and OpenAI
challenger on the same immutable prompt; only the primary reaches Atlas. The
factory remains off by default and requires explicit `PRAMANA_ASTRA_SHADOW_ENABLED`.
An API key being configured is not evidence that it is active. Source inspection
cannot establish the current host flag or that any recent pair completed.

`summarize_recorded_model_pairs` consumes existing `pramana.model_comparison.v1`
proof records only. It validates primary-only authority, half-open window, unique
pairs, declared prompt agreement, observed/start/completion timing and completed
stances; reports completion/refusal/status counts, stance agreement, known latency
and complete token-usage samples. Unusable pairs stay counted. No payload, prompt,
response ID, ticket or model output is echoed. Unknown usage/latency is not replaced
with a zero sample. Model confidences remain uncalibrated: this helper deliberately
produces no Brier score, investment winner, billed-dollar estimate, synthetic
counterfactual fills or additional model requests. Pair hashes are declarations,
not authentication or proof of identical system/model configurations. Actual AI
spend reconciliation remains with the durable budget/spend records.

## Finite acceptance and operating boundary

1. Hermetic synthetic tests must prove exact plan binding; fixed/dynamic bounds and
   membership; complete/unique cycles; past selection/input/forecast timing;
   shared fixed horizon/after-cost labels; half-open windows; pending/missing
   denominators; common-pair scoring and constant-half baseline; no input mutation.
2. Known malformed, null, duplicate, future, partial-cost or unauthorized records
   must fail closed. An empty dynamic list remains valid, never fills a quota.
3. Consume the actual existing challenger wrapper through mocked SDK/HTTP transport;
   verify no extra call and unchanged primary decision. Refused/mismatched model
   pairs cannot enter stance/latency comparisons or imply model skill.
4. Compare only frozen source/cost/risk profiles using approved recorded data and
   keep every refusal. Runtime freshness, held-position protection, subscriptions,
   order/fill/cost conservation and any portfolio returns require separate existing
   ledger/run-comparison qualification. A recorded proposal is not a fill.
5. Review source and exact-head hosted CI before inclusion in the single consolidated
   release. Installation has no activation/configuration side effect. A more-active
   PAPER profile that changes actual settings remains an explicit decision; risk,
   capital, loss/position limits, live-OFF, paid quota and credential settings remain
   unchanged by this build.

This module uses existing repository methods; no public framework code or new
license/data entitlement is imported. Trades provide evaluation evidence; they do
not automatically train or update Atlas. The finite follow-up is collecting and
reconciling approved recorded evidence, not an unbounded learning roadmap.
