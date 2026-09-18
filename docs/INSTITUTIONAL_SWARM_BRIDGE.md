# Explicit immediate institutional swarm route

Base: PR #140 at `47e2d5c28370a7bdab75c08ff4aca2c0935ffdf3`.
This is the first opt-in M03 operating-path bridge. It is not complete M03,
position-linked risk release, production deployment or live-money authorization.

## Actual route

`build_traded_runtime(institutional_inputs=..., oms=...)` constructs
`InstitutionalSwarmPaperTradingService`. The existing public synchronous and
asynchronous swarm APIs retain CIO, attribution, stress, Warden, existing-position,
maximum-position and cooldown checks. Approved dispatch then goes through the
InstitutionalPaperCoordinator, shared reservations, persisted execution programme,
durable OMS, paper broker and double-entry accounting. There is no call or fallback
to the default direct-submit implementation on this selected route.

`build_ghost_runner(..., institutional_inputs=...)` wires that same service into
the actual daemon factory, with the shared broker, OMS and Warden, the tracker's
snapshot provider, and the daemon's existing pilot pre-submit checks. Selection
requires pilot mode, bound instrument identity and matching accounting tenant.
The default and environment-only entry points remain unchanged: this is explicit
Python configuration, not automatic rollout to a running account.

## Required inputs, supported scope and refusal

The operator supplies persistent programme/accounting stores, an EdgePolicy,
SharedRiskPolicy, a versioned request provider, and factor/strategy/liquidity
providers. No calibrated edge, correlation, factor, cost, margin or available
liquidity is inferred from model confidence. Missing or inconsistent inputs refuse.
The provider receives isolated copies and cannot substitute the approved proposal,
capital plan, portfolio, tenant or observation time.

This component accepts only bound NSE INR cash equity/ETF, an IMMEDIATE plan and
one supplied volume bucket at the analysis time. Scheduled TWAP/VWAP/POV are refused
rather than left silently running or routed directly. The underlying planner still
supports them, but automatic due-slice lifecycle and common operating guards for
those later slices remain a separate M03 requirement.

## Last-boundary controls and evidence

The selected route rechecks daemon preflight, the live-money refusal, kill switch,
actual holdings, position limits, cooldown, stress and Warden immediately before
paper submission under the broker lock. Missing hooks or changed selected
coordinator providers refuse. The default coordinator's optional-hook-free behavior
is unchanged. The bridge does not hold the broker lock across programme admission.

The real swarm trace and declared input-provider revision are retained in the
immutable request provenance. Trace data must match that saved request before it
can accompany a fill. The coordinator supplies the authoritative child intent,
programme/slice and policy fields, and the broker commits the trace with its receipt,
positions, fees and cash. Caller trace data cannot overwrite those reserved fields.
The manifest distinguishes the selected route and declared policies/providers.
This is local configuration evidence, not authenticated provider or model provenance.

## Uncertainty, recovery and independent exits

A hold after a slice is claimed preserves the recovery fence. An exception is not
proof of no fill. Noncomplete execution is returned as recovery-required/uncertain,
never retried through the direct route. Explicit context reload and accounting
reconciliation use the existing original receipt without another submission.
The bridge does not perform automatic recovery or clear an uncertain claim.

Covered sales retain entry-evidence exemptions. Independent protective execution
remains outside the bridge and can close positions even when the request provider
is unavailable. Existing protective accounting can then reconcile the original exit.
The separate M01 reservation-release lifecycle remains unimplemented and unchanged.

## Verification boundaries

Synthetic tests exercise real pipeline specialists and deterministic CIO through
both public sync/async APIs into the institutional/accounting route. Factory tests
verify actual daemon preflight wiring and refusal without usable quotes; no broker
websocket, provider session or daemon background loop is started. Other cases cover
missing/altered inputs, final-boundary holds, trace mismatch, concurrent duplicate
calls, committed-fill recovery, accounting interruption and independent exits.
No existing acceptance assertion is removed or waived.

The Mac's remote tool was unavailable, so development used a separately verified
source snapshot in an isolated Linux container. Complete clean-runner CI, exact
commit IDs, test counts and any local environment failures are recorded in the PR.
No local test is evidence of genuine feeds, a qualified calibration model, effective
strategy performance, a target-host burn-in or production activation. Scheduled
programmes, authenticated operations, full lineage, shared-risk release, distributed
fencing and broader segment/settlement coverage remain in the delivery plan.

## Explicit recovery continuation

The public bridge now exposes `reconcile_program(program_id, tenant_id=...)` for
historical immediate-programme reconciliation without consulting current trading
providers or submitting another order. It returns original committed references,
not a new execution event or permission to trade. Source-trace correspondence and
completed-accounting postconditions are checked. See `INSTITUTIONAL_BRIDGE_RECOVERY.md`
for the tested scope, missing-receipt holds and remaining operational boundaries.

## Actual daemon-cycle verification

The bridge's final operating check now distinguishes its exact currently claimed
SUBMITTED child from unresolved historical OMS work. Initial admission/restart
still refuse all unresolved submissions; wrong or extra open orders, uncertain
states, changed intents and committed unrecorded fills are never exempted.
The original pilot checks still run at both stages. This repairs the self-blocking
case exposed by actual run_once integration rather than isolated service calls.
See `INSTITUTIONAL_DAEMON_CYCLE.md` for scope and synthetic end-to-end acceptance.
