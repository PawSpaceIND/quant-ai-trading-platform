# Position-linked shared-risk capacity

This component closes the used-position capacity gap in the selected paper
institutional path without deleting or rewriting approved reservation evidence.

## Two different numbers

`verify_shared_risk(...)["reservedLoss"]` remains the immutable historical
reservation total, less only the existing never-claimed cancellation releases.
It is intentionally not rewritten when a fill or exit occurs.

`position_linked_capacity(...)["effectiveReservedLoss"]` is the amount used
for new shared-account capacity decisions after the broker/journal pair has been
verified. It is derived each time from retained programmes and the internally
reconciled paper ledger. It is not an external broker balance assertion.

## Effective charge

The derivation first requires the existing shared-risk account/journal binding,
admission witnesses and committed BUY coverage to verify. Internal paper
reconciliation must report `matched`.

For each retained BUY reservation:

- a PENDING child remains fully charged;
- a DISPATCHING child with no committed broker receipt remains fully charged,
  because absence of a receipt at that instant is not permission to assume the
  uncertain submission cannot fill;
- a DISPATCHING, FILLED_UNACCOUNTED or EXECUTED child with the existing verified
  broker receipt contributes committed quantity;
- a FAILED or CANCELLED child with no broker fill contributes no future quantity;
- a journal slice labelled filled/executed without its broker BUY receipt refuses.

The original reservation amount is linear in parent quantity under the existing
risk model, so the per-share charge is the approved reservation divided by parent
quantity. Still-pending quantity uses that charge directly.

Committed position risk uses the paper ledger's reconciled current position. If
several retained entry programmes could own the remaining aggregate shares, those
shares are assigned to the **highest per-share reservation first**. This is
deliberately conservative: a later SELL is not assigned to a favourable historical
lot merely to free more capacity.

A complete reconciled close therefore produces zero effective position risk even
though the original reservation row remains in history. A partially closed
position releases only the disappearance of reconciled exposure. A failed
multi-slice programme keeps risk for the actual open shares but does not keep
charging a terminal child that never filled.

## Admission and dispatch

Preparation computes the existing effective charge while the programme-journal
transaction and broker lock are held, then applies the new parent reservation on
top. The reservation insert still commits with the new programme and slices.

Dispatch claims the child first. It then holds one programme-journal transaction,
checks the broker-derived effective charge, and runs the existing shared-risk
policy check. The same check is repeated later in the dispatch path as before.
No live-money route, limit, risk fraction or policy default changes.

Independent protective exits remain independent. Once their broker fill makes the
paper position reconcile closed, effective risk can fall. The existing protective
accounting reconciliation remains a separate pre-submit/recovery gate; this module
does not claim that a broker-position calculation is accounting completion.

## Restart and recovery

There is no new mutable release database. The charge is reconstructed from the
existing programme journal, immutable broker entry receipts and current reconciled
paper position after restart. Backup/restore therefore has no additional M01 store
to capture, but the existing ledger/journal correspondence must still pass.

The component deliberately refuses to infer capacity from a mismatched paper
ledger, missing binding or an executed journal child without broker evidence.
Local hashes and SQLite reconciliation do not authenticate an external brokerage
account and cannot detect coordinated privileged rewriting of every local source.

## Scope

The selected shared-risk implementation remains single-currency paper cash
equity/ETF. MCX and other margined segments use separate admission, margin and
settlement work and are not silently brought into this account-risk model.

This component does not implement multi-host fencing, external broker
reconciliation, live money, or a profitability claim.
