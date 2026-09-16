# Broker execution namespace closure

New imported execution IDs are SHA-256 identifiers over a versioned namespace containing
broker, tenant, account reference, exchange, trading day, broker order ID and raw trade ID.
They never include quantity or price, so changed economics cannot turn into a new fill.
The raw namespace is retained in the append-only OMS fill event. Repeated observation
and restart preserve idempotency; changed execution timestamps are discrepancies.

Legacy `KITE:exchange:trade` rows are not rewritten. They match only within the owning
order with its already-persisted account/contract binding, exact execution time and exact
quantity/price. Missing binding refuses `legacy_broker_fill_scope_unverified`. A legacy
partial fill may complete with new IDs, without replaying its historical quantity.

This change does not add multiple-account routing, broaden the instrument admission gate,
authenticate broker evidence, or establish decision-time contract identity. The latter is
an independent integration requirement. No network request or live-money route was added.

## Verification

Seventeen new synthetic regression cases; five failed on the pre-change implementation.
Focused broker/OMS set: 99 passed. Full Mac branch: 1447 passed, 13 existing deployment
portability failures, 14 subtests. Ruff and diff whitespace checks passed. Removing account
reference from the namespace caused its isolation regression to fail; production restored.

## Provider contract used

Kite Connect v3 documents `/orders` and `/trades` as day-scoped collections:
https://kite.trade/docs/connect/v3/orders/
It also documents derivative instrument-token reuse after expiry:
https://kite.trade/docs/connect/v3/market-quotes/
Neither observation is a reason to assume a raw ID is globally unique in our databases.
These public specifications are not evidence that a real account was reconciled.
