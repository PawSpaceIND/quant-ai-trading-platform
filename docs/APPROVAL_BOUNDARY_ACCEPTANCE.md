# Strict approval and readiness boundary acceptance

Ownership: #155. Base: `bf3812088e4308eb5372203669d9e07cd8e74562`.
This is a scoped repair, not a new live broker, promotion policy or readiness score.
The existing LIVE_MONEY_GATES.md, SUPER_PLATFORM_CLOSURE.md and PILOT_CLOSURE.md
remain authoritative for release acceptance.

## Reproduced defects and behavior

The generic compliance helper could interpret truthy approval values such as
`"false"` as approval, accepted untyped/unknown execution modes, and did not verify
the declared market/asset types. The broker router did not bind that context to
the actual order's market/asset class before passing it to the paper executor.
Readiness dataclasses accepted non-boolean statuses, duplicate or malformed check
names and caller-owned mutable inventories. These were reproduced on unchanged
main, with synthetic inputs. No real account or trade was used in reproduction.

The repaired compliance helper returns an actual False for malformed context,
mode, scope or approval flags. Correctly typed paper/research contexts preserve
the existing paper route. Both actual approval booleans are needed for the helper's
LIVE-policy result, but the router STILL refuses live execution unconditionally.
A true compliance result is not an authenticated approval or permission to trade.

The router rejects mismatched/untyped order scope before any broker call. Existing
InstrumentBoundOrderIntent subclasses remain supported; their deeper contract,
position, risk and execution checks are not replaced by this generic boundary.
The original LIVE exception and factory guards remain unchanged in effect.

ReadinessCheck now requires a canonical nonempty name, a real boolean status and
string detail. ReadinessReport validates typed checks, refuses duplicate names,
and snapshots list/tuple input into a tuple. Empty reports stay not ready. This
utility only aggregates the supplied checks: it does not know whether all required
release evidence was supplied, establish source truth, or grant trading authority.
It is not protection against arbitrary privileged Python object mutation.

## Verification

- New behavioral cases cover malformed approvals/context/modes, exact false
  approval behavior, both-side LIVE refusal, mismatched order scope, valid bound
  order compatibility, actual paper-order-ID preservation, and readiness typing,
  identity and inventory immutability.
- Nine independent guard removals use the EXISTING copied-source/offline harness
  in test_required_history_warmup_mutations.py. Each requires a passing control,
  then assertion failures without collection errors/skips; copies restore and all
  new protected source/test hashes remain unchanged.
- Inventory: docs/evidence/approval-readiness-guards.json.
- Existing compliance, readiness, live-adapter, live-guard and external/integration
  gate tests remain unmodified and are part of focused certification.
- Final test/commit/CI counts are recorded in the PR. A green test count is not
  production safety, investment performance or an independently verified approval.

The initial new mock returned an incomplete ExecutionResult constructor; two
fixture-only failures were corrected before the unchanged-source reproduction.
An import-order lint finding was fixed only in the new test module. No pre-existing
assertion, risk threshold, skip, expected-failure waiver or CI selection changed.

## Non-overlap and release work still owned elsewhere

These selected three existing paths were checked against all non-ancestor remote
branch deltas before edits. A new isolated clone preserves the primary checkout.
Ownership is published in #155 and coordinated on #146. Recheck peers before merge.

| Workstream | Existing ownership / next evidence |
|---|---|
| Claude truncation and diagnostics | #153; other agent owns the correction and real daemon acceptance. |
| Institutional execution/accounting and shared reservations | #136/#137/#140/#146 and the active position-linked-risk-release branch. |
| Operator recovery and complete audit retention | #148/#150; active persistent-operator-credentials branch. |
| Learning / data receipt / training lineage | #138/#139/#143/#145/#147. |
| Input qualification and macro freshness | #149. |
| Watchlist and broader instruments | #144; MCX #151. |
| This PR | Generic approval/router/readiness boundaries only. |

This patch does not complete real broker lifecycle, broker-side protection, static-IP
eligibility, source qualification, after-cost performance, independent alerts,
off-host restore, runtime migration, or integrated host/human/security acceptance.
Those remain blockers in the original release registers, not implicitly delegated
or marked complete by this table. No live transport or activation is introduced.

Before any eventual live release, the selected candidate must bind the exact code,
model, configuration and supported account/instrument scope; pass combined release
and fault tests; establish real-source/strategy/operations evidence; and receive
explicit owner approval. A successful merge, renamed status or a true boolean is
never substituted for that evidence. No AWS setting, credential, token, service,
ledger, budget, risk limit or existing agent implementation was changed here.
