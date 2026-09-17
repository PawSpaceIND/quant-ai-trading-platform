# Institutional accounting and execution-state recovery bundles

Continuation of draft #126 from `a89900352afa6ff5014c47a6add5a24f833dbf86`.
This extends offline recovery, not the operating trading path or its risk policy.
No live broker transport, model, automatic recovery or daemon startup is invoked.

## Gap reproduced

The earlier bundle could preserve a bound ledger and OMS but omit its separate
institutional accounting journal and execution-program database. The first test
reproduced that omission. Two further tests specified the missing positive and
pending-state restore paths; all three failed before implementation.

## Explicit inventory

For an existing bound NSE/INR equity/ETF institutional paper account, select both
stores along with its existing `oms` and seven ordinary sources. Example fields:

```json
{
  "oms": "/data/REPLACE_WITH_EXISTING_OMS.sqlite",
  "institutional_state": {
    "accounting": "/data/REPLACE_WITH_EXISTING_ACCOUNTING.sqlite",
    "programs": "/data/REPLACE_WITH_EXISTING_PROGRAMS.sqlite"
  }
}
```

The paths are explicit operator selections, not file discovery or new databases.
Partial pairs, missing files, aliases and overlapping sources are rejected.
Recorded institutional receipts require this inventory; schema-1/2/3 bundles that
omit it cannot restore those accounts as complete. Captures with the pair use
schema 4. Valid noninstitutional bundles retain their earlier schema and behavior.
A never-executed program cannot be discovered from a ledger with no receipt: its
operator must still select the complete inventory. No discovery claim is made.

## Read-only cross-store checks

`operations/institutional_recovery.py` opens existing SQLite files in read-only,
query-only transactions. It neither constructs writable broker/accounting/program
services nor mutates OMS schemas. Each selected component is bounded to 256 MiB;
row inventories are bounded, with at most 10,000 slices per program.

Accounting checks the canonical chart, journal metadata, contiguous posting
sequences, finite positive amounts, balance, orphan postings and transaction hashes.
An independent cash-fill replay compares exact trade, realized P&L and recorded
fee postings against the paper ledger, including starting capital and available
cash. Rehashing an economically incorrect trade does not make it acceptable.
Recorded costs are used; no fee rate, exchange rate or missing posting is invented.
This verifier explicitly refuses non-INR or non-cash accounting, including journals
whose settlement/adjustment/FX transactions cannot be mapped by this cash scope.

Programs retain full stored parent intent, slice quantities/schedules/states,
decision references and the original plan/context hashes. Checks conserve parent
quantity, validate raw integer fields, cross-check child OMS identities and bind
committed receipts to their program/slice, fill economics and historical cost basis.
The existing OMS replay remains required. Orphaned receipts, duplicate claims,
wrong parents, contradictory completion details and altered posting economics
refuse capture. Ordinary protective exits remain outside the scheduled OMS path;
the earlier protective evidence checks remain, and their cash postings are covered.

Missing accounting and unfinished slices remain discrepancies, not repair requests.
A pending dispatch without a receipt stays unresolved. Restore preserves original
states and halts and never posts a transaction, retries a trade or rewrites a path
pin. Complete recorded executions with missing core accounting are rejected rather
than treated as successfully accounted. No source or existing output is overwritten.

## Important nonclaims

The original full liquidity plan and risk-request payload are not persisted in the
program database, only their hashes and selected projections. Their hashes therefore
cannot be independently recomputed here: `planEvidenceVerified=false` and
`runtimeContextVerified=false` remain explicit. Every result also carries
`activationAuthorized=false`. A `restored` result means copied/checked captured
state, not permission to resume execution or a guarantee of profitable decisions.

The accounting/program paths are operator declarations; unlike the OMS they have
no pre-existing authoritative pin in the ledger. Local consistency is not source
authentication. Pre/post source and WAL fingerprints are retained, but the literal
writers-stopped flag is not process attestation or cross-host synchronization.
Uncommitted database changes are not represented as committed backup state.

## Verification and remaining work

The new file contains 64 synthetic regression cases. The focused recovery,
accounting, OMS, coordinator and protective-accounting suite passes 238 tests.
Exact arithmetic is pinned: a 100,000 opening balance and a 1,000 purchase restore
99,000 available cash and 1,000 securities cost, without another posting or order.
A protective sale, missing fee posting, unaccounted fill and unresolved dispatch
are also covered. Real rates, feeds, broker accounts and model calls are not used.

Three additional negative cases exposed contradictory metadata on DISPATCHING
slices during implementation; those cases failed before the guard was added.
Four independent guard-removal experiments (receipt-inventory requirement,
journal hash, ledger-to-posting economics, dispatch metadata) made their tests fail.
Experiments run in disposable interpreter memory, never by editing certified files.
The complete normal suite and unchanged failing risk acceptance are reported
separately with exact published-head CI evidence in the PR checkpoint.

The existing scheduled single-ledger backup remains unchanged. This change does
not add coordinated automatic backup, AI-registry/settlement-state inventory,
full plan/request reconstruction, authenticated approval, cross-host fencing,
path migration or actual off-host restore acceptance. The four risk-acceptance
repairs remain unapplied, and their CI job remains a failure rather than a waiver.
Full institutional-daemon assembly, broader segment support, qualified sources,
operational/security/human acceptance and forward after-cost results remain open.
No real account was captured, restored, recovered, activated or otherwise changed.
