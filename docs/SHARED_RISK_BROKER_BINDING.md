# Local paper broker / shared-risk journal binding

Continuation of existing PR #137, based on `3cb3e7f7ffd3923736f2e7994e43d12d6b1c3fd9`.
This completes and verifies an unpublished six-file account-binding candidate found
in the prior working copy. That working copy is preserved; certification uses a
separate checkout. This is not the previously blocked position-release operation.

## Scope

The selected local paper account retains an immutable journal UUID, canonical
ledger/journal paths, account/currency labels and shared-policy digest. Its account
row contains a separate immutable checksum witness. Deleting just the pin record
or its table cannot make the account appear unconfigured. New shared journals
receive an identity; old journal rows are not assigned invented identities.

Once pinned, the broker's final BUY transaction rejects an unlinked order. It opens
only the selected journal read-only, validates the UUID/policy/path pair and requires
an exact ACTIVE programme and DISPATCHING child, matching parent intent, quantity,
protective levels, strategy, client ID, idempotency key, policy authority and due time.
Unknown/inconsistent authority versions refuse. Another unresolved child also holds
new entry. This is additional to existing broker/order/protection controls, not a
replacement for Warden or current portfolio-risk evaluation.

A different journal, a copied journal at a different path or an empty replacement
at the same path cannot discard a pinned account's existing reservation history.
Other unselected tenants and existing legacy accounts keep their prior behaviour;
this code does not silently enable shared risk on a running account.

## Bootstrap and interrupted writes

The parent/reservation stays in the existing execution-journal transaction. The
broker pin and its witness commit together in the separate broker database. This
is NOT one cross-database atomic commit. If the broker pin commits and the journal
then rolls back or the process dies, new entries remain held; a new journal cannot
silently rebootstrap that account. Explicit operator recovery remains required.
Three subprocess-death tests cover before pin, after pin and after journal commit.
No crash test contacts a provider or operates on a real account.

Two initial coordinators targeting different journals cannot both bind the same
empty paper account. Cooperating coordinators sharing the correct journal retain
normal reservation serialization. These are local SQLite/process guarantees, not
cross-host fencing or authenticated external-broker account ownership.

## Historical evidence and exits

The broker adds its own binding checksum to committed entry receipts. Receipt reads
and offline institutional backup verification compare it with the immutable ledger
pin. Missing, changed or boolean substitute values refuse. These checks do not
require an online risk journal for historical receipt inspection. Restore preserves
the original paths, reports runtime rebinding required and grants no activation.

Covered sells and independent protective exits do not open the risk journal. They
remain executable when that journal is missing. No reservation is freed by this
feature, including after a protective exit or successful historical verification.

## Verification record

The 34-case unpublished binding file was preserved and tested; its backup fixture
used an incorrect top-level ledger key, now corrected to sources.ledger. Sixteen
additional certification cases check historical receipt binding, final authority
version, actual process death, competing journal selection and independent exits.
Together these are 50 new tests relative to the published #137 base.

A fresh archived base checkout reproduces three original binding failures. Six
new receipt/backup cases and two final-authority cases failed against the inherited
candidate, then passed after the missing checks were implemented. The focused
coordinator/reservation/policy/backup/protection/acceptance suite, including historical replay compatibility, passes 297 tests.
Four isolated in-memory guard removals reproduce the corresponding failures without
modifying certified source. Full-suite/CI results are recorded on the published PR.
Existing tests and the original 17-case risk acceptance file remain unchanged.

## Remaining boundaries

The pin is local provenance, not external account authentication, a cryptographic
signature, an authenticated operator approval or a hardware-backed identity. An
attacker able to rewrite all stores/witnesses is outside this check. A complete old
copy with the same UUID at the same path is not a globally fenced generation; full
rollback/replica detection and cross-host fencing remain separate work.

The final broker check verifies the claimed reserved child's identity; it does not
independently rerun every coordinator risk/allocation rule or authenticate the
caller that can directly alter local programme state. Full runtime/authenticated
operations and source qualification remain required before launch.

Position-linked capacity release remains unimplemented. The prior tool-blocked
release tests/operation were not retried or routed around in this continuation.
The existing never-claimed cancellation behaviour is unchanged. M01 stays partial;
no milestone is marked complete by this local binding patch.

No real account, credential, risk setting, running daemon, model approval or live
execution mode was changed. No runtime rollout, account migration or PR merge is
part of this checkpoint. The original unpublished working copy remains untouched.

The first complete run passed 2,694 tests and failed two historical-replay recovery
cases because the candidate created its optional binding table in every paper
ledger. The binding table is now created only inside successful explicit shared
setup; the ordinary replay table inventory remains unchanged. Two additional regressions
cover unselected and rejected setup, and the original replay assertions remain
unchanged. The prior run is retained as intermediate evidence, not waived.
