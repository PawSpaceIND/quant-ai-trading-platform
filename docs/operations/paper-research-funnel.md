# Paper research evidence and daily shortlist

This change observes decisions and provides an offline research shortlist. It does not
activate trading, change directives/subscriptions, alter execution tradability, risk,
capital, stops, provider permissions, AI caps or forecast mappings. It makes no profit
or live-readiness claim. No historical rows are backfilled.

## Session funnel

`python scripts/pilot_ops.py decision-funnel --database /private/path/paper.sqlite
--tenant paper --date YYYY-MM-DD` reads only. Run it against an authorized local copy or
through an authorized operator session; it has no scheduled/provider activity. Output
contains private decision/order evidence and belongs in a private operational report.

The half-open IST session and tenant are identical for evaluations and ledger fills.
One SQLite read transaction uses `mode=ro` and `query_only`; no broker/store constructor
or schema migration runs. Missing tables produce unknown counts, not an empty-book claim.
Journal outcomes partition recorded evaluations; inference/budget status and deterministic
gate reasons are separate overlapping dimensions, never additional evaluations. A side
is directional intent, not a submitted order. Execution state is carried as recorded;
uncertain dispatch must not be relabelled as a refusal or a retry instruction.

Each new journal row carries effective Atlas age budgets, voter/candidate counts, agent
ages and confidence, proof decision/hash references and execution state. Coverage candidates
describe the existing neutral/stale/zero-confidence predicates, not admitted consensus;
an earlier veto can prevent voting. Actual consensus participation remains unknown if it
never ran. Deterministic gate reasons describe the rule path before optional LLM/probe
overlays, not the final execution result. `over_atlas_age_budget` does not assert source
freshness: missing/future dependencies are separately handled by the existing pipeline.
The additive nullable journal column is migrated only by the existing writer on a future
approved deployment. Legacy observations remain unknown.

Fill links use exact tenant/order IDs and report swarm versus protection evidence, conflicts
and unattributed rows. A protective exit can legitimately have no swarm journal decision.
Evidence presence and IDs do not validate receipt hashes, cash conservation, an external
broker account or matching timestamps. Journal failures, unresolved submissions and
cross-window links still require investigation. This report is not an order retry tool.

Existing after-cost forecast scores use each stored horizon/cost and remain split by basis;
basis-integrity diagnostics accompany them. Unresolved observations are excluded explicitly.
The existing base-rate baseline is estimated on the scored sample, so it is descriptive,
not a frozen prospective predictor. Serially correlated observations, mixed/unverifiable
bases and insufficient samples cannot support promotion merely because a displayed score
beats a coin flip.

## Research shortlist: staged API, not a runtime universe change

`quant_ai.agents.scanner.research_shortlist` reuses the configured scanner's at-most100
NSE cash candidates and daily-history interface. There is no new engine or automatic
pre-open wiring. The caller supplies an aware evidence cutoff, target and previous exchange
session dates, the point-in-time universe, sectors and optional timestamped catalyst/spread
observations. Candidate membership and received times must be archived prospectively;
current survivors and retrospectively revised provider data are not historical evidence.

The API independently filters closed NSE daily bars and excludes target-day observations.
It requires21 prior bars including the specified previous session and known sectors.
It ranks five-session momentum, relative volume versus20 prior sessions and a20-session
close-times-volume turnover proxy. The initial explicit research policy is a50-million
rupee turnover proxy minimum, at most15 names, and at most3 per sector; these are research
screen parameters, not trading limits. Tie-breaking is deterministic. Insufficient evidence
can yield fewer than10, including zero; it never fills a quota by relaxing policy.

Catalysts/spreads are reported only when publication and receipt both precede the cutoff;
unknown remains unknown, and they are not scored in this initial component. Evidence
quality, spread freshness and catalyst impact need prospective validation before stronger
ranking claims. The fixed-list membership is preserved beside the shortlist; no comparison
results or probabilities are invented. Execution still requires separately qualified
identity/subscriptions, fresh prices/evidence, quorum, strategy, risk and protection gates.
Held-position monitoring must persist independently of tomorrow's research selection.

Kite daily research now accepts `tradable=False` NSE cash observations without changing
their identity/flag. Daily cache keys separate research and tradable identities. Minute
warmup admission remains strict. This fixes the offline-reproduced scanner/history mismatch;
it does not grant historical-data entitlement, widen instrument scope or make research
candidates tradable. Existing provider permissions/rate limits remain unchanged. Host
provider configuration/entitlement and full-universe coverage remain unverified.

## Evaluation gates

1. Week1: verify feed/protection continuity, journal coverage and fill reconciliation;
   track unknowns and stale/quorum/budget/strategy/risk causes with common denominators.
2. Week2: freeze shortlist policy, strategy/basis/cost assumptions and prospective simple
   baselines; compare fixed-list and shortlist research under equal predeclared constraints.
   Do not change strategy after seeing outcomes or infer independent samples from cadence rows.
3. Week3: independently reconcile paper cash/positions/fills, review after-cost calibration,
   stress/failure scenarios and unresolved evidence, then issue an explicit go/no-go verdict.

Three elapsed weeks alone cannot establish readiness. No capital-utilization target,
forced trading, paper order test, paid model/data call or live activation is introduced.
Deployment and any future runtime shortlist integration remain separately reviewed actions.
