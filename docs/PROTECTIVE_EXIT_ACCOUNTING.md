# Independent protective-exit accounting

This continues draft integration PR #126 from `e3b4986c601c7603ac0564fecbb27fab7861cdee`.
It adds no broker transport, live-money permission, strategy or new market admission.
The running daemon and real execution ledgers are not modified by these tests.

## Reproduced defects

Both a stop-loss and a take-profit could close the paper position while leaving the
separate accounting journal holding its original securities cost. Coordinator recovery
reported COMPLETE without recording the exit. A further negative test showed that deleting
the only protection-evidence row could make accounting incorrectly report no work.

## Receipt and durable obligation

The paper broker records its full order snapshot, actual prior quantity/cost basis and
contract identity alongside protective fills, just as for keyed coordinator submissions.
It also inserts `paper_protective_fill_outbox` in the same local transaction. This
append-only record binds the order, tenant and exact stored evidence hash. Receipt and
outbox are local integrity evidence, not exchange signatures or source authentication.
Missing, changed or legacy evidence refuses instead of disappearing from the work queue.
Existing historical rows are never backfilled with invented receipts or approvals.

The exit engine does not call accounting, acquire an accounting lock or wait for an
accounting response. An unavailable accounting database cannot suppress an exit. The
existing broker-database durability requirements still apply: this is not a promise that
a corrupted/full broker database can execute orders safely.

## Reconciliation

`ProtectiveExitAccounting` captures one consistent broker read snapshot, then releases
that read transaction before touching the accounting database. It independently replays
position cost and recorded margin movements. Every earlier non-protective fill must
already have exactly matching double-entry transactions. Missing strategy accounting,
changed postings, unsupported asset classes and unmapped journal transactions refuse.
Only the missing protective-exit postings may be written by this reconciler.

Cash sales release historical securities cost and record actual realized gain/loss.
Futures release recorded collateral and book actual realized P&L, never close notional.
Exact recorded cash fees are copied; no fee rate, exchange schedule or margin is guessed.
The implementation uses the existing accounting transaction IDs and posting conventions.
Repeated reconciliation validates existing records and cannot double-post an exit.

TradingJournal now exposes nested `atomic()` transactions backed by a writer lock and
savepoints. A protective sale and its fees commit together. An exception, or an abrupt
process exit before the outer commit, leaves them recoverable without partial posting.
Competing connections serialize on the same journal; this is not a distributed lease.

The existing institutional coordinator calls the reconciler before each scheduled slice
and after program accounting recovery. An unresolved result holds new coordinator work
before claiming the next slice. Independent protective exits remain separate and usable.
An additional broker commit during reconciliation yields PENDING, not a current match.
This is snapshot reconciliation, not one atomic transaction across broker and journal.

## Deliberate scope

The account must be declared INR/INR or USD/USD native/base currency with its original
paper capital. Mixed currencies, FX translation, unbound derivatives, options and other
asset classes are not inferred. Unbound cash scope uses its existing INDIA/USA market;
bound records must match the declared currency. This strict mirror does not import
external deposits, settlements or manual adjustments into the paper account.

The outbox is an optional accepted replay-recovery table so current backups preserve
it and older backups remain inspectable. Old exits lacking outbox/receipt evidence need
operator review; the upgrade does not claim to migrate them automatically. A reviewed
release/rollback plan and integration into the default daemon still remain required.

## Executable evidence

`tests/test_protective_exit_accounting.py` contains 25 synthetic cases. Coverage includes
stop/profit outcomes, exact repeated restart recovery, unavailable accounting, changed or
missing receipt evidence, immutable obligations, unaccounted predecessors, fee rollback,
blocked subsequent dispatch, futures collateral accounting, tenant and currency isolation,
wrong existing postings, competing writers and a broker advancing during reconciliation.
An abrupt subprocess exit occurs inside nested journal posting without cleanup; restart
recovers the committed broker exit without resubmitting it or duplicating the posting.

The focused accounting/coordinator/protection/margin regression set passes 126 tests.
Full baseline/candidate results, source hashes, sabotage and exact-head CI evidence are
recorded in the PR certification comment. No rate or real broker evidence is invented.

## Release boundaries retained

This is not default-daemon deployment, general multi-currency accounting, audited tax
accounting, exchange settlement, source authentication, cross-host scheduling or a proof
of profitability. Qualified feeds, real broker/contract-note/margin evidence, derivative
admission and lifecycle, target-host burn-in/restore/alerts, security/human acceptance and
the existing Mac deployment-script failures remain outside this specific closure.
