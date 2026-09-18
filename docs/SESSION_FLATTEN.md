# Finishing the day flat

An intraday mandate that cannot close its own positions is not an intraday mandate.

`quant_ai.risk.overnight` stops *new* exposure in the last ten minutes and caps what may be
carried into a gap. Nothing closed what was already open. A book that drifts into the night
because no component owned the last step is carrying gap risk nobody decided to take.

`quant_ai.risk.session_flatten` owns that step.

## Two calls, and they are not the same

```python
plan = plan_session_flatten(portfolio, market=Market.INDIA, now=now,
                            calendar=calendar, marks=marks)
# submit plan.orders through the normal governed OMS
proof = verify_flat(portfolio, working_orders=working, checked_at=after)
```

**A plan states what should be sent. Only the proof states what happened.** Treating
submission as completion is precisely how a position survives to the next morning while the
log records a clean close. `verify_flat` requires *both* zero open positions and zero
working orders — an empty book with a live order is a book about to have a position again.

## Deterministic, and it does not ask the AI

Same standing as a protective exit: a stop does not put its trigger to a vote. An engine
that can be talked out of going flat by a confident model has no intraday guarantee at all.

It bypasses the AI. It does **not** bypass the order path — the result is ordinary
`OrderIntent` values for the caller to submit through the same governed OMS, so every
covering order still passes the risk firewall, idempotency and the ledger. Bypassing the
AI is the point; bypassing the OMS is how a bug becomes an unrecorded trade.

## Reduce-only, enforced at construction

Every covering order is the exact opposite of the position it closes, with the exact same
size. `FlattenIntent` refuses to exist otherwise. That property is what makes it safe to run
without approval: it can only ever take exposure to zero — never reverse it, never add.

## Timing

The window opens **15 minutes** before the regular close, outside the overnight firewall's
10-minute entry block on purpose. By the time covering orders go out, exposure-adding
entries are already refused, so the book cannot be reopened behind the flatten. It also
leaves room for a retry on an order that does not fill.

## What it refuses to do

| Situation | Behaviour |
|---|---|
| No session calendar | `unavailable`. **Not** "nothing to do" — an unarmed control reporting nothing is indistinguishable from an armed one over an empty book. |
| Naive timestamp | `unavailable`. Where the book sits relative to the close is the whole question; a guessed answer is wrong half the year. |
| Calendar raises | `unavailable`. A fault does not pass the book through. |
| Position with no mark | Named in `unpriceable`, plan is `incomplete`. Pricing it at a guess would invent the trade's own execution reference. |
| Anything left open | `halt_required`. That is an unplanned overnight exposure — halt and alert, do not record the session as closed. |

## Verification

`tests/test_session_flatten.py` — 15 tests. Sabotage-verified: letting a missing calendar
report an empty plan as fine, pricing an unmarked position at a guess, calling a book flat
while orders are working, dropping the opposing-side assertion, and flattening before the
window each turn the named test red.
