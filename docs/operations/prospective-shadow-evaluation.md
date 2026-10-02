# Default-OFF prospective PAPER forecast evaluation

`learning.prospective` connects the actual daemon's completed decision snapshots to
an explicitly reviewed, frozen numeric candidate and the existing shadow forecast
journal/scoring components. It never trains, calls a provider, changes the primary
forecast, returns a trade proposal or changes sizing/risk. No candidate or calibrated
probability is supplied by this change. The primary Atlas and its v1 forecast mapping
keep their current behavior.

## Contract and scope

The strict `pramana.prospective_shadow_plan.v1` JSON contains exactly `schema`,
`frozen_at`, `bundle`, `cost_return`. The bundle uses existing typed dataset/training
manifests and artifact hashes. Its training time must precede the declared plan freeze,
which must precede the decision's input freeze. The reviewed plan hash is SHA256 of
canonical JSON, not the hash of a pretty-printed file. Compute with existing
`shadow._hash(shadow.decode(raw_plan))`. Hashes bind declarations; they do not
independently authenticate review, source rights or real availability.

The adapter accepts `pramana.shadow_logistic.v2` only. It adds an explicit,
label-digest-bound endpoint rule to the existing numeric logistic artifact:

- `endpoint_policy_id=frozen_quote_to_last_post_decision_tick_at_horizon.v1`;
- `maximum_endpoint_age_seconds`: strict integer 1–60;
- the existing fixed `horizon_seconds`, feature schema and cost-policy binding.

V1 artifacts/digests and inference remain unchanged. A v1 model cannot simply be
relabeled as v2: the artifact, label manifest and training-run hashes must bind the
reviewed new target. Existing offline fitting currently emits v1; this change does
not claim its outputs are automatically eligible or invent/retrain a v2 candidate.
Supplying a legitimate frozen v2 artifact with matching training semantics remains a
reviewed input prerequisite.

Supported feature names are `market_last_price`, `market_volume`, `market_bid`,
`market_ask`, `market_spread_bps`. They come only from the actual immutable Atlas
`feature_snapshot.market`, its Zerodha source and timestamp. The quote must precede
the snapshot cutoff and be <=60 seconds old; capture must occur <=120 seconds after
that cutoff and satisfy the candidate's own stricter feature-age rule. Missing,
malformed, crossed, future or stale values are refused. Fundamental/macro/technical
features do not acquire invented per-feature availability timestamps. Extending this
interface to those features requires explicit point-in-time provenance first.

The cost policy is `fixed_shadow_round_trip.v1`, with digest bound to exactly
`{"id":"fixed_shadow_round_trip.v1","cost_return":<the declared string>}`.
The fraction must be finite, nonnegative and <=1. It is a declared frozen evaluation
cost, **not measured trade fees/slippage or portfolio P&L**. Do not compare this target
to a primary forecast with different cost/horizon semantics as paired skill.

## Passive runtime behavior

The builder's default is OFF, including when plan/DB paths happen to exist. Explicit
opt-in requires exact `PRAMANA_PROSPECTIVE_SHADOW_ENABLED=true`, PAPER/pilot mode and
live money false, plus a private plan file, its reviewed canonical hash and a separate
private shadow DB:

- `PRAMANA_PROSPECTIVE_SHADOW_PLAN`;
- `PRAMANA_PROSPECTIVE_SHADOW_PLAN_SHA256`;
- `PRAMANA_PROSPECTIVE_SHADOW_DB`.

These are **not forwarded by Compose in this patch**. No runtime configuration,
mount, provider permission, identity transition or activation is performed. Invalid
optional configuration is logged/refused and does not prevent the original trading
runtime from starting. Never point the shadow DB at the ledger/OMS/budget stores;
existing non-shadow or populated offline journals are not silently adopted.

After existing decisions/protection/reconciliation, the daemon submits a bounded
copy (<=50 subjects, <=64KB each) to one worker without awaiting inference/disk on
the tick-ingestion loop. It preserves the proposal/journal decision ID and the
separate proof trace ID; missing IDs are refused, never substituted or stringified
as `None`. Busy workers reject another batch without spawning extra workers. The
status exposes failures and dropped captures since process start, and explicitly
keeps complete capture unverified. Those process-local counters do not prove
restart-wide capture completeness. Journal I/O failure cannot place, repeat or alter
an order. Shutdown cancels queued work; an already running bounded transaction may
finish its evidence-only commit.

Prediction uses the current recording clock, not a backdated replay timestamp. The
original input freeze/quote time remain attached to lineage. Input availability is
the actual worker capture time; exchange event time is not assumed to prove receipt
by the earlier snapshot cutoff. New forecasts and
capture receipts share the existing writer transaction. Exact retry is idempotent;
changed requests/candidate/plan cannot rewrite history. A repeated same-subject,
same-cutoff opportunity under a different ID is refused to avoid inflated samples.
The journal is append-only and bounded at 10,000 captured/refused inputs; reaching
the bound holds collection rather than pruning or resetting evidence.

## Outcomes, scoring and limitations

Once the fixed forecast horizon has elapsed, read only the accepted bounded buffer.
Use the last tick at/before that horizon, strictly **after** the forecast recording
time, within the artifact's endpoint freshness budget. Preserve its actual timestamp.
Never use a later tick, stretch the horizon, fetch a missing price or label a missing
endpoint as zero/loss. Missing/stale/evicted endpoints stay pending; continuous feed
and retention adequacy require actual qualification. There is no inference during
resolution. Persist the bound endpoint witness before settlement so a crash can
replay it after the original tick is evicted.

The shared forecast journal resolves gross opportunity return from the frozen input
quote, less the frozen declared cost. Decimal precision/rounding must preserve the
label's sign. Reports recheck model/input/receipt/endpoint bindings and show recorded,
refused, unlinked and pending counts. Candidate and constant-half Brier/log-loss/bin
metrics use identical resolved subjects. A descriptive 30-sample flag is not
calibration, independence, profitability or promotion. Quality/source-authenticity,
complete capture and trading authorization flags stay false.

`read_evaluation(path, raw_plan=..., reviewed_sha256=..., tenant_id=..., now=...)`
provides a constructor-free, WAL-aware read transaction with `query_only=ON` and no
migrations. The worker's `last_report`/`status()` are evidence-only observations.

Endpoint resolution visits at most 64 due pending forecasts per cycle in row-ID
round-robin order. A durable scheduling cursor advances before each attempted row, so an
unknown endpoint or interrupted page cannot starve later forecasts, including
after restart. Unresolved rows remain pending and are retried on wrap. This cursor
is mutable scheduling metadata, separate from the append-only evidence tables.

## Acceptance

Hermetic tests cover default OFF/no initialization; reviewed freeze/source/cost/time
and v1/v2 binding; no fabricated IDs; duplicate/restart idempotency; atomic forecast
receipt rollback; crash/replay of durable endpoint evidence; post-decision horizon
cutoff/freshness and unknown pending outcomes; after-cost signs/baseline scoring;
constructor-free reporting; bounded worker/failure/shutdown; actual daemon-hook
failure isolation and closed-session behavior. Existing Atlas overlay, sizing,
friction, selector, calendar, protection and runtime-fidelity guards remain required.
No real provider requests, broker trades, actual candidate promotion or host changes
are acceptance probes.

Dynamic opportunity selection already has its daemon hook. Its remaining separate
requirements are reviewed bound-v1 OMS identity/storage, authorized mapped/subscribed
NSE cash catalog <=50, current qualified 21-session history, later-cycle fresh ticks,
and preserved full held-position protection. Broad research names do not gain
execution tradability/entitlements automatically. This adapter changes none of those
requirements and supplies no identity migration or watchlist activation.
