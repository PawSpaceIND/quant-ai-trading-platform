# Execution recovery and full-intent hardening

This is an incremental extension of the existing decision-contract propagation design.
It preserves `pramana.order_intent_snapshot.v1`, the opt-in pipeline, existing contract
hashes and legacy cash-order identifiers. It is not a parallel order model or new PR.
The baseline is integration commit `0d4a3bc002c87cb4cf769c98a5f0e28bc54b382b`.

## Independently reproduced gaps

The existing integration already passed nine contract/restart identity cases. Six further
cases failed before this patch: changed protective levels accepted under one OMS identity;
mutated request inputs used after approval; approved parent payload overwritten; and
accounting recovery accepting a changed contract, quantity or unrelated OMS link.

## Implementation and evidence

| Requirement | Implemented enforcement | Executable evidence |
| --- | --- | --- |
| Full approved order survives restart | CREATED events retain the canonical full intent, including protective levels | Full bound intent round-trip and immutable event test |
| Idempotency means the same intent | Reusing an OMS decision with changed stops/targets refuses | Changed-protective-level regression |
| Event hashes are not enough | Replay checks full snapshot semantics against identity projection | Correctly hashed, inconsistent synthetic-writer test |
| Approved program identity cannot change | Immutable identity columns; complete parent snapshot validated at creation | Parent-payload overwrite rejection |
| Runtime request remains approved | Execution and accounting recovery recheck request fingerprint and parent snapshot | Mutated nested strategy-weight input refuses |
| Fill attribution survives accounting recovery | Check saved contract, side, units, market/class, exact OMS ID/intent, broker ID and price before posting | Wrong contract, quantity and OMS-link cases |
| Valid recovery still works | Bound paper fill is posted once after restart; no resubmission | Cash 100000 -> 99000; securities cost 1000; repeated recovery unchanged |
| Evidence remains traceable | Fill decision evidence contains approved-intent SHA-256 and contract snapshot | Exact evidence/OMS/ledger comparison |

The public `PaperLedgerEntry` reader now exposes its existing stored instrument identity
and validates it against the owning row. No historic identity is synthesized or rewritten.

`tests/test_execution_intent_closure.py` contains 23 regression cases. The pre-existing
legacy-program test still checks the same refusal but creates a legacy missing snapshot
at INSERT time; it no longer modifies a newly immutable approved payload.

## Legacy and release boundaries

Legacy OMS creation events that never stored full intent return unknown, and `get_intent`
refuses `oms_legacy_full_intent_unavailable`. That does not retroactively establish stops
or fill lineage. Strict accounting recovery requires a complete recorded intent; legacy
unaccounted fills need explicit operator review rather than invented approval evidence.

This does not prove every cross-database crash window, live broker execution, delivery or
settlement fidelity, fee/margin-source qualification, multi-account routing, default daemon
rollout, profitability or launch readiness. The prior capability/prerequisite and external
acceptance gates remain open. MCX pilot admission and all real-money routes remain closed.
Tests use synthetic local data and do not touch the operator's real execution ledger.
