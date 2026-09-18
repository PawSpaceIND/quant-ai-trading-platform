# Coordinator committed-fill crash recovery

Scope: local paper execution only, continuing the existing draft integration PR #126.
The baseline is `7e5402caf83497deb99859006e02c1eaa90c8d88`. Neither #127, #112,
main, the running daemon, nor the previously safety-blocked deployment candidate is changed.

## Failure reproduced

Six buy/sell cases left a committed paper fill outside accounting when the process failed
at broker return, OMS fill, or the execution-program marker. Recovery returned READY
without completing that fill. A separate adverse test demonstrated that a tampered
receipt cost basis could manufacture realized P&L; recovery now cross-checks prior fills.

## Durable sequence

1. Atomically claim the scheduled slice as DISPATCHING before OMS creation or submission.
   The claim serializes competing coordinators using the same program database.
2. Read current risk inputs and preserve every existing risk/intent/liquidity gate.
3. The paper broker commits an execution receipt in the same transaction as its fill,
   cash, position changes and idempotency claim. Prior units, average cost and contract
   identity are broker-owned facts, not caller-supplied recovery guesses.
4. Record the exact fill in the OMS, then FILLED_UNACCOUNTED, accounting and EXECUTED.
5. After a crash, lookup uses the exact tenant and program/slice idempotency key. Recovery
   verifies full intent, contract, program/sequence, OMS authority and historical basis.
   A committed receipt can repair the missing OMS/program projection without submission.

The journal's nested writes use savepoints so a nested method cannot commit its caller's
transaction. This is local atomicity, not a claim of one transaction across databases.
SQLite WAL documents that even ATTACHed databases are not atomic as a set:
https://www.sqlite.org/wal.html

## Fail-closed outcomes

DISPATCHING without a committed receipt is RECOVERY_REQUIRED, not READY or a fabricated
rejection. Recovery never sends an order. No automatic retry, timeout reset or inference
from symbol/price/quantity can release this uncertainty; explicit operator resolution is
still required. Coordinator dispatch for that tenant is held until recovery is resolved.
Independent protective exits remain available and have an executable regression.

A submit exception does not imply rollback: a wrapper may fail after commit. Such failures
retain the claim and OMS state for receipt-based recovery. Missing/ambiguous/legacy receipt
proof refuses. The original records are not rewritten and no historical intent is invented.

The pre-fill average is independently replayed from earlier fills for the same position,
including weighted buys, partial/full sells and contract identity. Later position state
cannot substitute for the cost basis that existed when the recovered fill committed.

## Executable coverage

`tests/test_coordinator_commit_recovery.py` contains 27 synthetic regression cases:
- Buy and sell crashes before OMS and program recording; exact repeat recovery.
- Abrupt subprocess `os._exit(73)` after broker, OMS, program, accounting and completion
  commits. No finally/close cleanup runs in that process.
- Crashes before broker submission remain unresolved and are never retried.
- An exception after broker commit is not rewritten as an order rejection.
- Wrong program, sequence, intent, prior basis, missing receipt and missing claim refuse.
- Competing database connections cannot both claim a slice.
- Partial fee posting recovers without duplicate trade/fee debits.
- Unaccounted slices block later dispatch while independent protection still operates.

## Local certification

The focused cross-module set passes 123 tests. The exact old baseline passes 1640 tests
with 13 Mac deployment-portability failures; the candidate passes 1667 with the exact
same 13 failures, independently compared from JUnit results. Python source/test hashes
were unchanged throughout both full runs. The restored candidate matches that manifest.
Removing receipt adoption, the recovery fence or the prior-basis comparison made each
corresponding behavioral regression fail. All mutations were restored and 123 tests
passed again. No test skip, xfail, invented rate or loosened risk cap was introduced.
Exact published SHA and Linux CI results are recorded in the PR certification comment.

## Remaining release boundaries

This does not close every possible crash/failure or certify real broker execution.
The no-receipt uncertain-dispatch case requires operator resolution; no automatic retry
or cancellation authority is invented. Multiple coordinators must share the same program
journal for the dispatch fence. Cross-host scheduling/lease fencing is not supplied here.

Legacy fills without these receipts are not automatically upgraded. Older releases do not
understand DISPATCHING: a reviewed migration/rollback plan remains required before rollout.
Independent protective exits are not automatically mirrored into this separate accounting
journal by this change; that pre-existing integration boundary remains open.

Default-daemon/release-manifest wiring, qualified feeds, genuine broker and contract-note
reconciliation, MCX admission, margin/delivery/settlement fidelity, target-host burn-in,
power-loss/restore drills, security review and human acceptance remain separate gates.
Abrupt test-process termination is not an off-host restore or a hardware durability test.
No actual trading session, forward calibrated edge, or profitability is claimed.
