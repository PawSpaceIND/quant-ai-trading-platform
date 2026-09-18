# Committed-entry coverage for a restored shared-risk journal

Continues PR #137 from `325a2681d56dd16c7c68eff317a22b35f3e83534`.
This is a read-only consistency/refusal guard. It does not implement position-linked
capacity release, change a risk limit, submit a new order or activate a daemon.

## Reproduced rollback gap

A journal backup can retain its original UUID, path and account policy while omitting
programmes created after the backup. The current broker ledger may still contain
committed purchases attributed to those programmes. Identity matching alone does
not prove that the journal retains all risk associated with those purchases.

Three tests run against a separate archive of the published baseline demonstrated:
a new programme was admitted after this rollback; the broker/journal pair appeared
valid; and a separately queued, claimed child could reach the final broker despite
the missing earlier committed programme. All three failed before the coverage guard.

## Implementation

The broker/journal pairing now checks every recorded BUY in the selected account.
The joined broker evidence must identify the same programme, slice, tenant, order,
idempotency claim, policy authority and broker binding. The full intended child and
contract must match its retained parent and slice. Fill quantities, recorded price,
notional, timestamps and expected cash-asset metadata are checked independently.

Every committed entry must retain a valid DISPATCHING, FILLED_UNACCOUNTED or EXECUTED
slice with its expected client/broker identity. DISPATCHING with an already committed
receipt is deliberately valid: it represents an interruption recoverable from the
receipt, not permission to submit another trade. Contradictory history refuses.

Coverage runs during ordinary pairing, coordinator admission, the final broker
entry check and existing offline recovery pairing. Disabling path comparison for a
copied recovery bundle never disables this history check. The bounded read refuses
more than 100,000 committed entries instead of silently truncating the account.
Malformed/duplicate JSON fields and inconsistent source schema, typed amounts,
parent fields or cash-collateral metadata refuse. No database is rewritten.

## Preserved operations

Normal restart, a committed-but-unrecorded fill and its accounting recovery remain
valid. An earlier filled slice remains valid when a later slice legitimately fails.
Independent protective exits and their accounting work despite an incomplete risk
journal; new entries remain held. History remains required after the position is
closed. A flat account does not erase its prior committed-entry obligations.

## Certification provenance

An interrupted local working tree already contained the first coverage guard and
25 prospective cases. These two files were copied into a separate checkout using
verified hashes; the original dirty files were not reset, overwritten or published.
The separate published-baseline archive reproduced the three rollback failures.

A further 21 certification cases were added. Fourteen initially failed against the
recovered candidate, exposing incomplete schema/subject, numeric-type, parent-state,
chronology and cash-metadata checks. The repaired focused cross-module suite passes
335 cases. Existing published tests and acceptance assertions remain unchanged.

Four isolated interpreter-memory guard removals are checked against behavioral
regressions; they do not edit the certified production source. Full-suite, exact
commit and completed CI evidence are recorded in the PR after observation.

## Remaining M01 boundaries

This closes the named case where the broker ledger retains committed buys while
its same-identity journal omits or contradicts them. It is NOT universal snapshot
rollback detection. Lost never-executed reservations, simultaneous rollback of all
stores, external broker authority and distributed cross-host fencing remain open.
Hashes are not signatures and do not defeat an actor able to forge all local state.

No OMS/fee/accounting completion is inferred from this pairing check. Those have
separate verification paths. Reservations stay charged after filled positions close;
verified position-linked release is still unimplemented. No release test or release
implementation from the earlier blocked operation was retried through this patch.

M01 remains partial in the existing 12-milestone plan. Full request/plan persistence,
institutional-daemon/accounting assembly and real-source/host/security/forward
acceptance remain separate requirements. The changes are tested with disposable
synthetic accounts only, with no actual account, credential, daemon or model change.
