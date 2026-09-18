# Shared paper risk reservations — admission and unclaimed cancellation

Stacked on PR #136 at `c42db823a0583a501faf0ff0345f514d00107e71`.
This is part of M01 in `REMAINING_DELIVERY_PLAN.md`, not full account-risk closure.
The operating daemon is unchanged and no real account risk fraction was selected.

## Implemented behavior

An explicitly supplied `SharedRiskPolicy` declares the synthetic/operational paper
account reference, INR or USD currency, account modeled-loss fraction and revision.
There is no default account fraction. Bootstrap requires an empty ledger and no
previous programs; historical risk is not inferred or silently migrated.

Reservations share the existing execution-program SQLite database. The immutable
policy, exact parent/slices and full-parent reservation commit in one transaction.
Separate coordinator connections serialize admission through the same journal.
A replay of the same decision does not reserve twice. Different strategies draw
from the same account allowance. The policy is pinned to the supplied account,
tenant and selected ledger/journal paths; changing or omitting it holds new entries.

Charges use the existing full-parent model: maximum of stop-distance loss and
notional times declared adverse return plus costs. Saved adverse/cost inputs and
amounts are independently checked against parent intent during replay. Dispatch
rechecks total reserved loss against current supplied equity and checks policy
again immediately before OMS submission. A failed new reservation rolls back the
new parent/slices rather than leaving an apparently approved unreserved program.

A never-claimed PLANNED program may be cancelled under the journal transaction.
All its slices become CANCELLED and an immutable release record frees capacity
exactly once. Claimed, partially filled, uncertain or completed programs cannot use
this release API. Cancellation does not call a broker or retry an order.

## Recovery and deliberate boundaries

Offline institutional recovery accepts the exact three added tables and replays
policy, reservation, release, model and program bindings read-only. Unknown/partial
tables, orphan or missing records and changed hashes refuse. Covered sales and
independent protective exits remain outside entry-only shared capacity checks.

The full reservation stays charged after a filled program completes, even if the
position later closes. Automatic position-linked deallocation is NOT implemented.
This deliberately prevents invented free capacity but is not the final lifecycle.
It needs exact fill/position ownership and reconciled release evidence before use
in an unattended long-running account. M01 therefore remains partially complete.

All participating coordinators must use the same journal. This is local database
serialization, not cross-host fencing. Paths and account references are operator
declarations, not broker-authenticated global account identities. Binding the
selected risk journal authoritatively in the broker ledger and preventing bypass
through the legacy/direct broker path remain runtime-integration requirements.
The account cap does not independently authenticate supplied equity or market data.

Only same-currency cash equity/ETF requests at base_rate=1 are supported. No FX,
short exposure, margin requirement, fee rate or external trading evidence is guessed.
No shared risk budget is enabled in the actual daemon by this PR. The cancellation
method is a Python component API, not an authenticated operator endpoint.

## Verification scope

52 new synthetic cases cover the shared exact boundary, competing coordinator
connections, distinct strategies, idempotency, restart, policy omission/change,
provider-time mutation, unused cancellation, pending/partial/complete holds,
covered exits, immutable/corrupt records, atomic rollback, abrupt process exit
before/after commit and offline backup replay. The focused cross-module run passes
265 tests, including the original institutional acceptance and protection tests.

Four isolated guard removals are caught by their behavioral regressions: aggregate
capacity, final policy check, independent model-amount replay and record immutability.
The certified executable files are unchanged by those in-memory experiments.
The original risk-acceptance file, existing tests, CI selection and deployment
configuration are not altered. Exact full-suite/CI evidence belongs to the PR head.

The initial three tests specified a missing API rather than demonstrating three
independent defects in an existing shared-risk implementation. The backup integration
first rejected its new tables; support was added only for the exact complete schema,
with missing/unknown metadata still refused. No source credentials were accessed.

This work also adds the explicit 12-milestone delivery plan requested by the user.
A module's passing tests do not close its missing lifecycle or deployment evidence.


## Subsequent local broker-binding checkpoint

The continuation in `SHARED_RISK_BROKER_BINDING.md` adds immutable broker-ledger
journal identity/path selection, a final exact-claimed-child check and historical
receipt/backup linkage. The account cannot discard reservations merely by selecting
another journal or issuing an unlinked BUY through the current paper broker API.
This supersedes the earlier absence of local ledger enforcement, not the remaining
external-account authentication, complete rollback/cross-host fencing or runtime
acceptance requirements. Position-linked capacity release is still unimplemented.
