# Broker execution identity closure

Scope: read-only broker observations updating a local OMS. No order transport, permission
to trade, real account attestation or MCX pilot admission is added.

## Identity and source attribution

New imported fills use `KITE2:<sha256>` over a versioned identity containing provider,
tenant, account reference, exchange, trading day, broker order ID and exchange trade ID.
Provider trade numbers are not assumed to be globally unique across accounts or dates.
The identity is persisted in the append-only FILL event, not reconstructed from economics.

A correctly computed hash alone is insufficient: the OMS independently requires the
source to match its previously persisted broker/account/order/exchange binding and tenant.
The declared day must match the fill timestamp in Asia/Kolkata. Reserved KITE2 IDs cannot
be written without source metadata. Event replay repeats the attribution checks against
the binding established earlier in the event stream. Hashes are integrity checks, not
provider signatures or proof that a capture came from a real account.

## Legacy compatibility

Existing `KITE:<exchange>:<tradeId>` history is never rewritten. It is reconciled only when
that same local order already has a matching saved account/contract binding and the
recorded quantity, price, broker order and timestamp match. A previously unbound legacy
fill refuses `legacy_broker_fill_scope_unverified`. A legacy partial fill may be completed
using new namespaced fills; restart and repeated captures must not duplicate either.

Callbacks without provider trade lineage remain callbacks. Equal aggregate quantities and
prices do not manufacture a mapping; ambiguous additional fills still refuse.

## Evidence and remaining boundary

`test_broker_fill_namespace.py` covers account/tenant/day separation, restart idempotency,
source persistence, timestamp restatements, malformed sources and legacy recovery.
`test_external_fill_authority.py` adds wrong-account, wrong-exchange, wrong-day, missing
binding and missing-source rejection, plus replay of semantically invalid hashed events.
All fixtures are synthetic. Five source-attribution cases failed before the guard repair.

This does not add multi-account execution routing within a tenant. Selection and
compatibility remain bounded by the existing reconciler and saved binding. Full immutable
decision-time contract propagation is a separate integration requirement, not supplied by
observing a broker token after the fact. No actual market fills or profits are claimed.

Official API reference consulted: Kite Connect v3 Orders describes `/orders` and `/trades`
as daily collections and distinguishes API placement from confirmed execution:
https://kite.trade/docs/connect/v3/orders/
