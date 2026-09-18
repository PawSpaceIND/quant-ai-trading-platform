# Actual immediate institutional daemon-cycle acceptance

Continues PR #146 from `9a7f9d9094e70a9c59647e2365fe19449e4a234d`.
This closes the demonstrated self-blocking final preflight and adds actual
factory/scheduler/daemon-cycle coverage. It does not add scheduled execution,
authenticated recovery, risk-capacity release or a production rollout.

## Reproduced integration blocker

A synthetic test constructed the actual build_ghost_runner factory, seeded its
real TickBuffer, and ran AutonomousTradingDaemon.run_once with the real pipeline,
deterministic CIO, Warden, institutional provider and configured pilot checks.
After valid admission, the institutional coordinator marked its own child SUBMITTED
in the OMS. The last pilot check then saw an open order and treated that very child
as unresolved historical work. It halted with runtime_identity_oms_recovery_required
before any broker fill. Previous direct service and factory-only tests missed this.

The original failing actual-cycle log is retained. Fixture corrections used the
existing seed_capital/paper_order_ids API names and a synthetic alternating price
path: a monotonically rising sample legitimately yielded an overbought non-entry.
No strategy threshold or existing test assertion was changed to force a trade.

## Exact current-submission context

The factory binds a dedicated final-phase preflight to the institutional runtime.
Initial admission still invokes the original pilot pre-submit check with no
in-flight context. At final dispatch, the bridge derives the exact child and OMS ID
from its own prepared programme. Tenant, decision, single-slice identity, parent
intent, DISPATCHING state and recorded client ID must all agree.

Runtime identity verification permits precisely one open OMS order only for that
final-phase context. It must be the exact SUBMITTED intent with zero filled quantity,
no broker ID and no fill price. All OMS history is still verified, and every existing
nonprotective ledger fill must still match its completed OMS record. Therefore an
already committed but unrecorded fill cannot use the exception to run again.

Any unrelated open order, uncertain state, changed quantity, wrong tenant/client ID,
missing programme claim or corrupted history holds entry. Restart supplies no final
context and continues to refuse outstanding submissions. This is not a blanket
pending-order exclusion, a retry permission or a mechanism for clearing uncertainty.

The final check still enforces market session, fresh prices, source manifest,
portfolio controls, kill switch and Warden under the existing broker lock. The
additional hook is bound once, keeps the original initial hook, and appears in the
institutional configuration fingerprint. Replacing the selected initial hook is
rejected. The default noninstitutional path retains its prior behavior.

## End-to-end synthetic evidence

The 24 new cases run actual daemon cycles through real pipeline specialists and
CIO decisions, programme admission, shared-risk binding, OMS, broker receipts,
fees, double-entry accounting, decision history and briefs. The positive path is
also run with required sector/correlation/expected-shortfall gates armed and supplied
with the existing synthetic closed-history provider. Test assertions verify those
gates are armed and their history is ready before accepting the trade.

Negative cases cover missing institutional inputs, initial unresolved submissions,
post-submission OMS corruption/uncertainty, new unrelated orders, stale quotes,
entry halts, changed runtime policy, claim tampering and changed preflight hooks.

A second cadence does not repeat the filled entry. An interruption in accounting
preserves the original broker fill as uncertain, and explicit historical recovery
reconciles it without another order. The actual protection_tick closes exposure
while halted; subsequent explicit reconciliation matches trade/fee accounting and
retains the halt. Off-hours cycles do not create institutional programmes.

These tests never start an SDK connection, websocket supervisor or paid-model call.
Quotes, news/fundamentals, calibration, liquidity and historical prices are explicitly
synthetic. They exercise the real deterministic decision and operating code, not
LLM skill, authentic broker reconciliation or after-cost investment performance.
Four isolated in-memory mutations test final-phase wiring, the unresolved-order
fence, exact intent comparison and claimed-child validation. Exact frozen-tree
results and CI evidence are recorded on #146; earlier partial runs are preserved.

## Remaining release boundaries

The opt-in Python configuration is still required. Environment-only rollout,
authenticated operator selection, scheduled TWAP/VWAP/POV lifecycle, autonomous
recovery policy, M01 position-linked risk release, distributed fencing and actual
source/model/host/human/security/forward-performance acceptance remain open.
The context is local data and not a signed operator authorization. Cross-database
operations are not claimed to be a single distributed transaction. No running
account, daemon, credential, model approval or live-money setting is changed here.
